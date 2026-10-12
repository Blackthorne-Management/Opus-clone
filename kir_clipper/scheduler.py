"""Post planning (slots, caps, spacing) and the publish loop (submit, poll, retry, alert)."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import db
from .config import Config
from .notify import notify
from .publishers import DryRunPublisher, PublishError

log = logging.getLogger("kir.scheduler")

ACTIVE_POST_STATES = ("planned", "submitted", "manual", "posted")
LATE_REPLAN_HOURS = 6       # planned posts older than this are moved to a fresh slot, not fired late
STUCK_PENDING_HOURS = 24    # a submitted post still pending this long after publish_at is a failure


def _tz(cfg: Config) -> ZoneInfo:
    return ZoneInfo(cfg.get("publishing.upload_post.timezone") or "UTC")


def _slots(cfg: Config) -> list[time]:
    out = []
    for s in cfg.get("publishing.slots") or ["12:00"]:
        hh, mm = str(s).split(":")
        out.append(time(int(hh), int(mm)))
    return sorted(out)


def next_slot(conn, cfg: Config, platform: str, now: datetime, lead_minutes: int = 20,
              not_before: datetime | None = None, trial: bool = False) -> datetime:
    """Earliest posting slot for `platform` that respects the daily cap and minimum spacing.

    Trial reels have their own cap (`max_posts_per_day.<platform>_trial`) and spacing, since they are
    shown to non-followers and don't crowd your followers' feed."""
    tz = _tz(cfg)
    caps = cfg.get("publishing.max_posts_per_day") or {}
    cap = int(caps.get(f"{platform}_trial", 3) if trial else caps.get(platform, 2))
    gap = timedelta(minutes=int(cfg.get("publishing.min_gap_minutes", 180)))
    rows = conn.execute(
        f"SELECT publish_at FROM posts WHERE platform=? AND trial=? AND status IN ({','.join('?' * len(ACTIVE_POST_STATES))})"
        " AND publish_at >= ?",
        (platform, int(trial), *ACTIVE_POST_STATES, db.iso(now - timedelta(days=2))),
    ).fetchall()
    taken = [db.parse_iso(r["publish_at"]) for r in rows]
    per_day: dict[date, int] = {}
    for t in taken:
        d = t.astimezone(tz).date()
        per_day[d] = per_day.get(d, 0) + 1

    earliest = max(now + timedelta(minutes=lead_minutes), not_before or now)
    day = earliest.astimezone(tz).date()
    for _ in range(800):
        if per_day.get(day, 0) < cap:
            for slot in _slots(cfg):
                candidate = datetime.combine(day, slot, tzinfo=tz)
                if candidate < earliest:
                    continue
                if any(abs(candidate - t) < gap for t in taken):
                    continue
                return candidate.astimezone(ZoneInfo("UTC"))
        day += timedelta(days=1)
    raise RuntimeError(f"No free posting slot found for {platform}")


def plan_posts(conn, cfg: Config, clip_id: int, now: datetime | None = None,
               platforms: list[str] | None = None) -> list[int]:
    now = now or db.utcnow()
    platforms = platforms or list(cfg.get("publishing.platforms") or [])
    existing = {r["platform"] for r in conn.execute(
        "SELECT platform FROM posts WHERE clip_id=? AND trial=0 AND status IN ('planned','submitted','manual','posted')",
        (clip_id,))}
    ids = []
    for p in platforms:
        if p in existing:
            continue
        when = next_slot(conn, cfg, p, now)
        ids.append(db.insert_post(conn, clip_id, p, when, step="feed"))
        log.info("Planned clip %s on %s at %s", clip_id, p, when.astimezone(_tz(cfg)).strftime("%a %d %b %H:%M"))
    db.log_event(conn, "posts_planned", clip_id, {"platforms": platforms})
    return ids


def mark_posted(conn, post_id: int, url: str | None = None) -> None:
    """Owner confirms a hand-posted (trial) reel went out."""
    post = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if post is None or post["status"] not in ("manual", "planned"):
        raise ValueError("Only posts waiting to be posted by hand can be marked posted")
    db.update_post(conn, post_id, status="posted", posted_at=db.iso(db.utcnow()), platform_post_id=url)
    db.log_event(conn, "post_marked_posted", post_id, url)
    _refresh_clip_status(conn, post["clip_id"])


def copy_for(clip: dict, platform: str) -> tuple[str, str]:
    """(title, caption) for a platform, falling back to the general caption + hashtags."""
    pc = (clip.get("platform_captions") or {}).get(platform) or {}
    title = (pc.get("title") or clip.get("title") or clip.get("hook") or "").strip()
    caption = (pc.get("caption") or clip.get("caption") or title).strip()
    tags = [t if t.startswith("#") else f"#{t}" for t in (clip.get("hashtags") or [])]
    if platform in ("tiktok", "instagram") and tags and "#" not in caption and clip.get("kind", "short") == "short":
        caption = f"{caption}\n\n{' '.join(tags)}"
    if platform == "youtube" and clip.get("kind", "short") == "short" and "#shorts" not in caption.lower():
        caption = f"{caption} #shorts"
    if platform == "x":
        caption = caption[:270]
    return title, caption


def _days_ahead(cfg: Config, platform: str) -> int:
    limits = cfg.get("publishing.max_days_ahead") or {}
    return int(limits.get(platform, limits.get("default", 365)))


def _fail(conn, cfg: Config, post, error: str, retryable: bool, now: datetime) -> None:
    attempts = post["attempts"] + 1
    backoff = cfg.get("publishing.retry_backoff_minutes") or [5, 20, 60]
    max_attempts = int(cfg.get("publishing.max_attempts", 4))
    if not retryable or attempts >= max_attempts:
        db.update_post(conn, post["id"], status="failed", attempts=attempts, last_error=error, next_attempt_at=None)
        db.log_event(conn, "post_failed", post["id"], error)
        notify(cfg, f"Post failed: clip {post['clip_id']} on {post['platform']}",
               f"Gave up after {attempts} attempt(s). Last error: {error}", level="alert")
    else:
        wait = timedelta(minutes=int(backoff[min(attempts - 1, len(backoff) - 1)]))
        db.update_post(conn, post["id"], status="planned", attempts=attempts, last_error=error,
                       next_attempt_at=db.iso(now + wait), request_id=None)
        db.log_event(conn, "post_retry", post["id"], {"error": error, "attempt": attempts})
        log.warning("Post %s failed (%s); retry %d in %s", post["id"], error, attempts, wait)


def _refresh_clip_status(conn, clip_id: int) -> None:
    states = [r["status"] for r in conn.execute("SELECT status FROM posts WHERE clip_id=?", (clip_id,))]
    clip = db.get_clip(conn, clip_id)
    if not clip or clip["status"] in ("rejected",):
        return
    if "posted" in states:
        new = "posted"
    elif states and all(s in ("submitted", "manual", "posted", "failed", "cancelled") for s in states):
        new = "scheduled"
    else:
        return
    if clip["status"] != new:
        db.update_clip(conn, clip_id, status=new)


def publish_tick(conn, cfg: Config, publisher, now: datetime | None = None) -> dict:
    """Submit due posts, poll submitted ones, retry failures. Safe to call as often as every few seconds."""
    now = now or db.utcnow()
    stats = {"submitted": 0, "posted": 0, "retried": 0, "failed": 0, "replanned": 0, "held": 0}
    if cfg.dry_run:
        stats["held"] = conn.execute("SELECT COUNT(*) FROM posts WHERE status='planned'").fetchone()[0]
        return stats

    planned = conn.execute(
        """SELECT p.*, COALESCE(p.file_path, c.file_path) AS media_path, c.status AS clip_status
           FROM posts p JOIN clips c ON c.id = p.clip_id
           WHERE p.status='planned' AND (p.next_attempt_at IS NULL OR p.next_attempt_at <= ?)
           ORDER BY p.publish_at""", (db.iso(now),)).fetchall()
    for post in planned:
        if post["clip_status"] not in ("approved", "scheduled", "posted"):
            continue
        publish_at = db.parse_iso(post["publish_at"])
        trial_mode = cfg.get("cadence.trial_mode", "manual")
        if post["trial"] and trial_mode != "api":
            # Trial reels you post by hand: at the scheduled time, remind the owner and wait for "Mark posted".
            if publish_at <= now:
                db.update_post(conn, post["id"], status="manual")
                db.log_event(conn, "post_manual_due", post["id"], {"file": post["media_path"]})
                notify(cfg, f"Time to post a Trial Reel (clip {post['clip_id']}, {post['step']})",
                       f"Post this as an Instagram Trial Reel, then click 'Mark posted' in the dashboard:\n"
                       f"{post['media_path']}")
                stats["manual"] = stats.get("manual", 0) + 1
            continue
        if publish_at - now > timedelta(days=_days_ahead(cfg, post["platform"])):
            continue  # too far ahead for this platform's scheduler; submit on a later tick
        if now - publish_at > timedelta(hours=LATE_REPLAN_HOURS):
            # Machine was off or posting was paused: take a fresh slot instead of a late burst.
            db.update_post(conn, post["id"], status="cancelled", last_error="slot missed; replanned")
            new_at = next_slot(conn, cfg, post["platform"], now, trial=bool(post["trial"]))
            new_id = db.insert_post(conn, post["clip_id"], post["platform"], new_at, step=post["step"],
                                    trial=bool(post["trial"]), file_path=post["file_path"])
            db.log_event(conn, "post_replanned", post["id"], {"new_post": new_id})
            stats["replanned"] += 1
            continue
        clip = db.clip_dict(db.get_clip(conn, post["clip_id"]))
        title, caption = copy_for(clip, post["platform"])
        when = publish_at if publish_at > now + timedelta(minutes=2) else None
        kwargs = {"title": title}
        if post["trial"]:
            kwargs["extra"] = dict(cfg.get("cadence.trial_api_fields") or {})
        try:
            job = publisher.schedule(post["media_path"], caption, [post["platform"]], when, **kwargs)
        except PublishError as exc:
            _fail(conn, cfg, post, str(exc), exc.retryable, now)
            stats["retried" if exc.retryable else "failed"] += 1
            continue
        except FileNotFoundError:
            _fail(conn, cfg, post, f"clip file missing: {post['media_path']}", False, now)
            stats["failed"] += 1
            continue
        db.update_post(conn, post["id"], status="submitted", request_id=job, attempts=post["attempts"] + 1,
                       next_attempt_at=None, last_error=None)
        db.log_event(conn, "post_submitted", post["id"], {"job": job, "platform": post["platform"]})
        stats["submitted"] += 1
        _refresh_clip_status(conn, post["clip_id"])

    submitted = conn.execute("SELECT * FROM posts WHERE status='submitted' AND publish_at <= ?",
                             (db.iso(now),)).fetchall()
    for post in submitted:
        checker = DryRunPublisher() if str(post["request_id"]).startswith("dry-") else publisher
        st = checker.status(post["request_id"])
        if st.state == "posted":
            db.update_post(conn, post["id"], status="posted", posted_at=db.iso(now),
                           platform_post_id=st.platform_post_id or st.url)
            db.log_event(conn, "post_live", post["id"], {"platform": post["platform"], "url": st.url})
            stats["posted"] += 1
            _refresh_clip_status(conn, post["clip_id"])
        elif st.state == "failed":
            _fail(conn, cfg, post, st.error or "failed", True, now)
            stats["retried"] += 1
        elif now - db.parse_iso(post["publish_at"]) > timedelta(hours=STUCK_PENDING_HOURS):
            _fail(conn, cfg, post, "still pending 24h after publish time", True, now)
            stats["retried"] += 1
    return stats
