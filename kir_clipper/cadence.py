"""'Post everything five times': turns one approved clip into a series of posts.

  1. Trial Reel        day 0    (Instagram, shown to non-followers first)
  2. Feed Reel         day 0    (later slot than the trial; every feed platform)
  3. Mirrored Trial    day 1    (horizontally flipped picture, captions still readable)
  4. Feed + 90 days    \
  5. Feed + 180 days   /  created by the re-edit job (reedit.py), max `reedit.max_reposts`

Steps 1-3 are configurable under `cadence.steps`. `timeline()` gives the dashboard every step with its
status and time, including projected dates for reposts that don't exist yet, for the countdowns.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db
from .config import Config
from .highlights import clip_words
from .notify import notify
from .render import default_recipe, render_clip
from .scheduler import _tz, next_slot, plan_posts

log = logging.getLogger("kir.cadence")

STEP_LABELS = {
    "trial": "Trial Reel",
    "feed": "Feed Reel",
    "mirrored_trial": "Mirrored Trial Reel",
}


def step_label(name: str, cfg: Config) -> str:
    if name and name.startswith("repost_"):
        n = int(name.split("_")[1])
        return f"Feed Reel +{int(float(cfg.get('reedit.interval_days', 90)) * n)} days"
    return STEP_LABELS.get(name or "feed", (name or "feed").replace("_", " ").title())


def enabled(cfg: Config) -> bool:
    return bool(cfg.get("cadence.enabled", False))


def render_variant(cfg: Config, conn, clip: dict, changes: dict, suffix: str) -> Path:
    """Render another version of a clip (e.g. mirrored) from the original recording."""
    stream = conn.execute("SELECT * FROM streams WHERE id=?", (clip["stream_id"],)).fetchone()
    transcript = json.loads((cfg.subdir("transcripts") / f"{stream['hash']}.json").read_text(encoding="utf-8"))
    recipe = {**(clip.get("recipe") or default_recipe(cfg, clip)), **changes}
    out = cfg.subdir("clips") / f"stream_{clip['stream_id']:04d}" / f"clip_{clip['id']:05d}_v{clip['version']}_{suffix}.mp4"
    render_clip(cfg, Path(stream["file_path"]), clip, clip_words(transcript["words"], clip["start"], clip["end"]),
                recipe, out)
    return out


def schedule_step(cfg: Config, conn, clip_id: int, name: str, platforms: list[str], now: datetime,
                  not_before: datetime | None = None, trial: bool = False, file_path: str | None = None) -> list[tuple[int, datetime]]:
    """Put one cadence step on the calendar for each platform (skipping platforms it is already on)."""
    existing = {r["platform"] for r in conn.execute(
        "SELECT platform FROM posts WHERE clip_id=? AND step=? AND status NOT IN ('cancelled','failed')", (clip_id, name))}
    out = []
    for platform in platforms:
        if platform in existing:
            continue
        when = next_slot(conn, cfg, platform, now, not_before=not_before, trial=trial)
        out.append((db.insert_post(conn, clip_id, platform, when, step=name, trial=trial, file_path=file_path), when))
        log.info("Clip %s step %s on %s%s at %s", clip_id, name, platform, " (trial)" if trial else "",
                 when.astimezone(_tz(cfg)).strftime("%a %d %b %H:%M"))
    return out


def _local_midnight(dt: datetime, tz: ZoneInfo, plus_days: int) -> datetime:
    d = dt.astimezone(tz).date() + timedelta(days=plus_days)
    return datetime.combine(d, time(0, 0), tzinfo=tz)


def plan_clip(cfg: Config, conn, clip_id: int, now: datetime | None = None) -> list[int]:
    """Plan every post for a newly approved short clip (or a 90-day re-edit of one)."""
    now = now or db.utcnow()
    row = db.get_clip(conn, clip_id)
    clip = db.clip_dict(row)
    if not enabled(cfg) or clip.get("kind", "short") != "short":
        return plan_posts(conn, cfg, clip_id, now)
    tz = _tz(cfg)

    if clip["version"] > 1:  # steps 4 and 5: a re-edit going back out to the feed
        step = f"repost_{clip['version'] - 1}"
        ids = []
        for platform in cfg.get("reedit.platforms") or cfg.get("publishing.platforms") or []:
            when = next_slot(conn, cfg, platform, now)
            ids.append(db.insert_post(conn, clip_id, platform, when, step=step))
        db.log_event(conn, "cadence_planned", clip_id, {"step": step, "posts": ids})
        return ids

    mode = cfg.get("cadence.trial_mode", "manual")
    existing = {(r["step"], r["platform"]) for r in conn.execute(
        "SELECT step, platform FROM posts WHERE clip_id=? AND status NOT IN ('cancelled','failed')", (clip_id,))}
    ids: list[int] = []
    anchor: datetime | None = None        # time of the first post: "day 0"
    last_same_day: datetime | None = None
    recipe = clip.get("recipe") or {}
    hook_a, hook_b = (recipe.get("hook_text") or "").strip(), (recipe.get("hook_text_alt") or "").strip()
    ab = bool(cfg.get("ab_hooks.enabled", True)) and hook_b and hook_b.lower() != hook_a.lower()
    made_by_step: dict[str, list[tuple[int, datetime]]] = {}
    for step in cfg.get("cadence.steps") or [{"name": "feed", "day": 0}]:
        name, day, trial = step["name"], int(step.get("day", 0)), bool(step.get("trial"))
        if trial and mode == "skip":
            continue
        file_path = None
        if step.get("mirror"):
            # A/B test: the mirrored trial reel carries the alternative hook.
            changes = {"mirror": True, **({"hook_text": hook_b} if ab else {})}
            try:
                file_path = str(render_variant(cfg, conn, clip, changes, "mirror"))
            except Exception as exc:
                log.exception("Mirrored render failed for clip %s", clip_id)
                notify(cfg, f"Couldn't render the mirrored version of clip {clip_id}", str(exc)[:500], level="alert")
                continue
        if day == 0:
            not_before = (last_same_day + timedelta(minutes=1)) if last_same_day else None
        else:
            not_before = _local_midnight(anchor or now, tz, day)
        platforms = [p for p in (step.get("platforms") or cfg.get("publishing.platforms") or [])
                     if (name, p) not in existing]
        made = schedule_step(cfg, conn, clip_id, name, platforms, now, not_before, trial, file_path)
        made_by_step[name] = made
        ids += [i for i, _ in made]
        times = [t for _, t in made]
        if times:
            anchor = anchor or min(times)
            if day == 0:
                last_same_day = max(times)
    if ab and made_by_step.get("trial") and made_by_step.get("mirrored_trial"):
        from .hooktests import create
        create(conn, clip_id, made_by_step["trial"][0][0], made_by_step["mirrored_trial"][0][0], hook_a, hook_b)
    db.log_event(conn, "cadence_planned", clip_id, {"posts": ids})
    return ids


def timeline(cfg: Config, conn, clip_id: int) -> list[dict]:
    """Every step for a clip's lineage: done, scheduled, waiting for you, or projected. Times in UTC ISO."""
    row = db.get_clip(conn, clip_id)
    root_id = row["parent_clip_id"] or row["id"]
    lineage = conn.execute("SELECT * FROM clips WHERE id=? OR parent_clip_id=? ORDER BY version",
                           (root_id, root_id)).fetchall()
    steps: dict[str, dict] = {}
    order: list[str] = []
    for c in lineage:
        for p in conn.execute("SELECT * FROM posts WHERE clip_id=? AND status NOT IN ('cancelled') ORDER BY publish_at",
                              (c["id"],)):
            name = p["step"] or (f"repost_{c['version'] - 1}" if c["version"] > 1 else "feed")
            if name not in steps:
                steps[name] = {"step": name, "label": step_label(name, cfg), "clip_id": c["id"], "posts": []}
                order.append(name)
            steps[name]["posts"].append({"id": p["id"], "platform": p["platform"], "status": p["status"],
                                         "publish_at": p["publish_at"], "posted_at": p["posted_at"],
                                         "trial": bool(p["trial"])})
    out = []
    for name in order:
        s = steps[name]
        states = {p["status"] for p in s["posts"]}
        if states <= {"posted"}:
            s["status"] = "posted"
        elif "manual" in states:
            s["status"] = "post_by_hand"
        elif "failed" in states:
            s["status"] = "failed"
        else:
            s["status"] = "scheduled"
        s["at"] = min(p["posted_at"] or p["publish_at"] for p in s["posts"])
        s["platforms"] = sorted({p["platform"] for p in s["posts"]})
        out.append(s)

    # Projected reposts (steps 4 and 5) that the re-edit job hasn't created yet.
    if enabled(cfg) and row["kind"] == "short" and out:
        interval = timedelta(days=float(cfg.get("reedit.interval_days", 90)))
        first = min(db.parse_iso(s["at"]) for s in out)
        for n in range(1, int(cfg.get("reedit.max_reposts", 2)) + 1):
            name = f"repost_{n}"
            if name in steps:
                continue
            if (lineage[0]["note"] or "").startswith("no_repost"):
                break
            out.append({"step": name, "label": step_label(name, cfg), "status": "projected",
                        "at": db.iso(first + interval * n), "posts": [],
                        "platforms": list(cfg.get("reedit.platforms") or [])})
    out.sort(key=lambda s: s["at"])
    for i, s in enumerate(out, start=1):
        s["number"] = i
    return out
