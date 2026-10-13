"""A/B tests of on-screen hooks (trial reel = hook A, mirrored trial reel = hook B)."""

from __future__ import annotations

import logging
from datetime import timedelta

from . import db
from .config import Config
from .performance import snapshot

log = logging.getLogger("kir.hooktests")


def create(conn, clip_id: int, post_a: int, post_b: int, hook_a: str, hook_b: str) -> int:
    cur = conn.execute(
        "INSERT INTO hook_tests(clip_id, post_a, post_b, hook_a, hook_b, created_at) VALUES (?,?,?,?,?,?)",
        (clip_id, post_a, post_b, hook_a, hook_b, db.iso(db.utcnow())))
    return cur.lastrowid


def _views(conn, post_id: int, days: float):
    snap = snapshot(conn, post_id, days=days)
    if snap is None:  # fall back to the latest numbers (e.g. entered by hand a bit later)
        snap = conn.execute("SELECT * FROM performance WHERE post_id=? AND views IS NOT NULL "
                            "ORDER BY fetched_at DESC LIMIT 1", (post_id,)).fetchone()
    return snap["views"] if snap else None


def evaluate(conn, cfg: Config, now=None) -> int:
    """Decide every test whose two posts are old enough and have numbers. Returns how many were decided."""
    now = now or db.utcnow()
    days = float(cfg.get("ab_hooks.decide_after_days", 3))
    min_diff = float(cfg.get("ab_hooks.min_difference", 0.15))
    decided = 0
    for t in conn.execute("SELECT * FROM hook_tests WHERE winner IS NULL").fetchall():
        posts = {r["id"]: r for r in conn.execute("SELECT * FROM posts WHERE id IN (?,?)", (t["post_a"], t["post_b"]))}
        a, b = posts.get(t["post_a"]), posts.get(t["post_b"])
        if not a or not b or a["status"] in ("cancelled", "failed") or b["status"] in ("cancelled", "failed"):
            conn.execute("UPDATE hook_tests SET winner='void', decided_at=? WHERE id=?", (db.iso(now), t["id"]))
            continue
        if not (a["posted_at"] and b["posted_at"]):
            continue
        if now - db.parse_iso(b["posted_at"]) < timedelta(days=days):
            continue
        va, vb = _views(conn, a["id"], days), _views(conn, b["id"], days)
        if va is None or vb is None:
            continue
        hi, lo = max(va, vb), max(1, min(va, vb))
        winner = "tie" if hi / lo - 1 < min_diff else ("a" if va > vb else "b")
        conn.execute("UPDATE hook_tests SET views_a=?, views_b=?, winner=?, decided_at=? WHERE id=?",
                     (va, vb, winner, db.iso(now), t["id"]))
        db.log_event(conn, "hook_test_decided", t["clip_id"], {"a": t["hook_a"], "b": t["hook_b"], "winner": winner})
        decided += 1
    return decided


def winning_hook(conn, clip_id: int) -> dict | None:
    t = conn.execute("SELECT * FROM hook_tests WHERE clip_id=? AND winner IN ('a','b') ORDER BY id DESC LIMIT 1",
                     (clip_id,)).fetchone()
    if not t:
        return None
    win, lose = ("a", "b") if t["winner"] == "a" else ("b", "a")
    return {"winner": t[f"hook_{win}"], "loser": t[f"hook_{lose}"], "views_winner": t[f"views_{win}"],
            "views_loser": t[f"views_{lose}"]}


def lessons(conn, cfg: Config) -> str:
    """Recent decided tests, as text Claude can learn from."""
    rows = conn.execute("SELECT * FROM hook_tests WHERE winner IN ('a','b') ORDER BY decided_at DESC LIMIT ?",
                        (int(cfg.get("ab_hooks.lessons", 8)),)).fetchall()
    out = []
    for t in rows:
        win, lose = ("a", "b") if t["winner"] == "a" else ("b", "a")
        out.append(f'WON "{t[f"hook_{win}"]}" ({t[f"views_{win}"]:,} views) vs LOST "{t[f"hook_{lose}"]}" '
                   f'({t[f"views_{lose}"]:,} views)')
    return "\n".join(out)


def summary(conn) -> dict:
    rows = [dict(r) for r in conn.execute("SELECT * FROM hook_tests ORDER BY id DESC LIMIT 50")]
    decided = [r for r in rows if r["winner"] in ("a", "b", "tie")]
    return {"running": sum(1 for r in rows if r["winner"] is None), "decided": len(decided),
            "a_wins": sum(r["winner"] == "a" for r in decided), "b_wins": sum(r["winner"] == "b" for r in decided),
            "ties": sum(r["winner"] == "tie" for r in decided), "recent": rows[:15]}
