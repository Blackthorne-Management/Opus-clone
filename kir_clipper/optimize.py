"""Best posting times from your own results.

For each platform, every feed post's views 3 days after posting are grouped by the local hour it went
out. An hour's "lift" is how its typical (median) views compare with the platform's typical views, on a
log scale so one viral outlier can't dominate. Hours need at least `min_posts_per_hour` posts to count.
Suggestions keep the same number of daily slots, respect the minimum gap, and (optionally) keep one slot
exploring an untested hour next to the best one, so the schedule keeps learning.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections import Counter, defaultdict

from . import db
from .config import Config
from .performance import snapshot
from .scheduler import _slots, _tz

log = logging.getLogger("kir.optimize")


def hour_stats(conn, cfg: Config, platform: str) -> tuple[list[dict], int]:
    tz = _tz(cfg)
    rows = conn.execute(
        """SELECT p.id, p.posted_at FROM posts p JOIN clips c ON c.id = p.clip_id
           WHERE p.platform=? AND p.trial=0 AND p.status='posted' AND c.kind='short' AND p.posted_at IS NOT NULL""",
        (platform,)).fetchall()
    by_hour: dict[int, list[float]] = defaultdict(list)
    minutes: dict[int, Counter] = defaultdict(Counter)
    total = 0
    for r in rows:
        snap = snapshot(conn, r["id"], days=3)
        if not snap or snap["views"] is None:
            continue
        local = db.parse_iso(r["posted_at"]).astimezone(tz)
        by_hour[local.hour].append(math.log1p(max(0, snap["views"])))
        minutes[local.hour][local.minute // 15 * 15] += 1
        total += 1
    if not total:
        return [], 0
    overall = statistics.median([v for vs in by_hour.values() for v in vs])
    out = []
    for hour, vals in sorted(by_hour.items()):
        med = statistics.median(vals)
        out.append({"hour": hour, "posts": len(vals), "median_views": round(math.expm1(med)),
                    "lift": round(math.exp(med - overall), 2), "minute": minutes[hour].most_common(1)[0][0]})
    return out, total


def suggest(conn, cfg: Config, platform: str) -> dict:
    min_hour = int(cfg.get("optimize.min_posts_per_hour", 3))
    min_total = int(cfg.get("optimize.min_posts", 20))
    explore = bool(cfg.get("optimize.explore", True))
    gap_h = int(cfg.get("publishing.min_gap_minutes", 180)) / 60
    current = [s.strftime("%H:%M") for s in _slots(cfg, platform)]
    hours, total = hour_stats(conn, cfg, platform)
    result = {"platform": platform, "posts_with_data": total, "hours": hours, "current": current,
              "suggested": current, "enough": total >= min_total, "explore_slot": None}
    trusted = sorted([h for h in hours if h["posts"] >= min_hour], key=lambda h: -h["lift"])
    if not result["enough"] or not trusted:
        return result

    def far(h, chosen):
        return all(min(abs(h - c), 24 - abs(h - c)) >= gap_h for c in chosen)

    n = len(current)
    picks: list[tuple[int, int]] = []
    for h in trusted:
        if h["lift"] < 1.0 and len(picks) >= max(1, n - 1):
            break
        if far(h["hour"], [p[0] for p in picks]):
            picks.append((h["hour"], h["minute"]))
        if len(picks) == n - (1 if explore and n > 1 else 0):
            break
    tested = {h["hour"] for h in hours}
    if explore and n > 1 and picks:
        best = picks[0][0]
        for delta in sorted((d for d in range(-12, 13) if d), key=abs):  # nearest untested hour that fits
            cand = (best + delta) % 24
            if cand not in tested and 7 <= cand <= 23 and far(cand, [p[0] for p in picks]):
                picks.append((cand, 30))
                result["explore_slot"] = f"{cand:02d}:30"
                break
    for s in current:  # top up with existing slots if data couldn't fill them
        if len(picks) >= n:
            break
        hh, mm = (int(x) for x in s.split(":"))
        if far(hh, [p[0] for p in picks]):
            picks.append((hh, mm))
    result["suggested"] = sorted(f"{h:02d}:{m:02d}" for h, m in picks)
    return result


def suggestions(conn, cfg: Config) -> list[dict]:
    return [suggest(conn, cfg, p) for p in (cfg.get("publishing.platforms") or [])]


def weekly(conn, cfg: Config) -> list[str]:
    """Weekly job: apply (if auto_apply) or report better posting times. Returns lines for the summary."""
    from . import settings as settings_mod
    lines = []
    for s in suggestions(conn, cfg):
        if not s["enough"] or s["suggested"] == s["current"]:
            continue
        if cfg.get("optimize.auto_apply", False):
            settings_mod.update(cfg, {f"publishing.slots_by_platform.{s['platform']}": s["suggested"]})
            lines.append(f"{s['platform']}: posting times changed to {', '.join(s['suggested'])} based on your results")
        else:
            lines.append(f"{s['platform']}: your results suggest posting at {', '.join(s['suggested'])} "
                         f"(now {', '.join(s['current'])}). Apply it in the dashboard's Stats tab.")
    return lines
