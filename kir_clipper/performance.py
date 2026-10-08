"""Performance tracking: record real results per post and compare them with the virality score."""

from __future__ import annotations

import csv
import statistics
from pathlib import Path

import numpy as np

from . import db

METRICS = ("views", "likes", "comments", "shares", "watch_time")


def record(conn, post_id: int, **metrics) -> None:
    vals = {k: metrics.get(k) for k in METRICS}
    conn.execute(
        "INSERT INTO performance(post_id, views, likes, comments, shares, watch_time, fetched_at) VALUES (?,?,?,?,?,?,?)",
        (post_id, *vals.values(), db.iso(db.utcnow())),
    )


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
            record(conn, int(post_id), **metrics)
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
