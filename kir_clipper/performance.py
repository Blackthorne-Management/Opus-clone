"""Performance tracking: record real results per post and compare them with the virality score."""

from __future__ import annotations

import csv
import json
import logging
import statistics
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from . import db

log = logging.getLogger("kir.performance")

METRICS = ("views", "likes", "comments", "shares", "watch_time", "saves", "reach")
CHECKPOINT_DAYS = (1, 3, 7, 30)   # when to snapshot a post's numbers after it goes live
FETCH_PER_RUN = 20                # Upload-Post allows 100 analytics calls per 5 minutes


def record(conn, post_id: int, source: str = "manual", raw=None, at=None, **metrics) -> None:
    vals = {k: metrics.get(k) for k in METRICS}
    conn.execute(
        f"INSERT INTO performance(post_id, {', '.join(METRICS)}, source, raw, fetched_at) "
        f"VALUES ({', '.join('?' * (len(METRICS) + 4))})",
        (post_id, *vals.values(), source, json.dumps(raw)[:20000] if raw is not None else None,
         db.iso(at or db.utcnow())),
    )


def due_posts(conn, now: datetime, checkpoints=CHECKPOINT_DAYS) -> list:
    """Posted posts that have passed a checkpoint since their last snapshot (oldest checkpoint first)."""
    rows = conn.execute(
        """SELECT p.*, (SELECT MAX(fetched_at) FROM performance f WHERE f.post_id = p.id) AS last_fetch
           FROM posts p WHERE p.status='posted' AND p.posted_at IS NOT NULL""").fetchall()
    out = []
    for r in rows:
        posted = db.parse_iso(r["posted_at"])
        last = db.parse_iso(r["last_fetch"]) if r["last_fetch"] else None
        passed = [posted + timedelta(days=d) for d in checkpoints if posted + timedelta(days=d) <= now]
        if passed and (last is None or last < passed[-1]):
            out.append(r)
    out.sort(key=lambda r: r["posted_at"])
    return out


def fetch_due(conn, cfg, publisher, now: datetime | None = None, limit: int = FETCH_PER_RUN) -> int:
    """Snapshot the numbers for posts that hit a checkpoint. Returns how many were recorded."""
    now = now or db.utcnow()
    if publisher is None or not hasattr(publisher, "metrics"):
        return 0
    done = 0
    for post in due_posts(conn, now)[:limit]:
        if not post["request_id"]:
            continue  # posted by hand: numbers are entered in the dashboard
        try:
            m = publisher.metrics(post["request_id"], post["platform"])
        except Exception as exc:  # rate limited or service hiccup: try again next run
            log.info("Analytics not available right now (%s)", exc)
            break
        if m:
            record(conn, post["id"], source="upload_post", at=now, **m)
            done += 1
    if done:
        log.info("Recorded performance for %d posts", done)
    return done


def snapshot(conn, post_id: int, days: float = 3.0, tolerance: float = 0.6):
    """The numbers recorded closest to `days` after posting, if one falls within `tolerance` x days of it."""
    post = conn.execute("SELECT posted_at FROM posts WHERE id=?", (post_id,)).fetchone()
    if not post or not post["posted_at"]:
        return None
    target = db.parse_iso(post["posted_at"]) + timedelta(days=days)
    best = None
    for r in conn.execute("SELECT * FROM performance WHERE post_id=? AND views IS NOT NULL", (post_id,)):
        gap = abs((db.parse_iso(r["fetched_at"]) - target).total_seconds())
        if gap <= days * tolerance * 86400 and (best is None or gap < best[0]):
            best = (gap, r)
    return best[1] if best else None


def import_csv(conn, path: Path) -> int:
    """CSV columns: post_id OR (platform + platform_post_id), then any of views,likes,comments,shares,watch_time."""
    n = 0
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            post_id = row.get("post_id")
            if not post_id:
                found = conn.execute("SELECT id FROM posts WHERE platform=? AND platform_post_id=?",
                                     (row.get("platform"), row.get("platform_post_id"))).fetchone()
                if not found:
                    continue
                post_id = found["id"]
            metrics = {k: float(row[k]) if k == "watch_time" else int(float(row[k]))
                       for k in METRICS if row.get(k) not in (None, "")}
            record(conn, int(post_id), source="csv", **metrics)
            n += 1
    return n


def _ranks(x: list[float]) -> np.ndarray:
    order = np.argsort(x)
    ranks = np.empty(len(x))
    ranks[order] = np.arange(len(x))
    return ranks


def score_report(conn) -> str:
    rows = conn.execute(
        """SELECT c.score, p.platform, MAX(pf.views) AS views FROM performance pf
           JOIN posts p ON p.id = pf.post_id JOIN clips c ON c.id = p.clip_id
           WHERE pf.views IS NOT NULL GROUP BY p.id""").fetchall()
    if not rows:
        return "No performance data yet. Import results with `kir perf-import results.csv`."
    lines = [f"{len(rows)} posts with view data", "", "score band | posts | median views"]
    for lo in range(90, -10, -10):
        band = [r["views"] for r in rows if lo <= r["score"] < lo + 10]
        if band:
            lines.append(f"   {lo:2d}-{lo + 9:2d}   | {len(band):5d} | {statistics.median(band):,.0f}")
    if len(rows) >= 8:
        scores = [r["score"] for r in rows]
        views = [r["views"] for r in rows]
        rho = float(np.corrcoef(_ranks(scores), _ranks(views))[0, 1])
        lines += ["", f"Rank correlation between score and views: {rho:+.2f}",
                  "Near zero means the score is not predicting performance yet: tune virality_criteria.md." if abs(rho) < 0.2
                  else "The score is tracking performance; adjust review.auto_post_threshold with the bands above."]
    return "\n".join(lines)
