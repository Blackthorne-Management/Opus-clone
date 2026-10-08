"""90-day re-edit job: re-render previously posted clips with a substantially different treatment.

A clip "lineage" is the original clip plus every re-edit of it (children point at the original via
parent_clip_id). A lineage is eligible when its most recent post went live `interval_days` ago or more
and nothing from it is currently waiting in the queue.
"""

from __future__ import annotations

import itertools
import json
import logging
import random
import statistics
from datetime import datetime, timedelta
from pathlib import Path

from . import db
from .captions import CAPTION_STYLES
from .config import Config
from .highlights import PLATFORMS, REFINE_SCHEMA, clip_words
from .pipeline import gate
from .render import CROP_MODES, ZOOM_MODES, default_recipe, render_clip
from .scheduler import plan_posts

log = logging.getLogger("kir.reedit")

# Visual fields that define how a render looks. A re-edit must differ from every earlier version in at
# least MIN_DIFF of these, and the hook text must be new.
VISUAL_FIELDS = ("caption_style", "crop_mode", "zoom", "caption_position", "watermark_corner")
MIN_DIFF = 3
OPTIONS = {
    "caption_style": CAPTION_STYLES,
    "crop_mode": CROP_MODES,
    "zoom": ZOOM_MODES,
    "caption_position": ("lower", "middle"),
    "watermark_corner": ("tr", "tl"),
}


def _diff(a: dict, b: dict) -> int:
    return sum(1 for f in VISUAL_FIELDS if a.get(f) != b.get(f))


def vary_recipe(previous: list[dict], base: dict, rng: random.Random | None = None) -> dict:
    """Pick the visual combination that differs most from all earlier recipes (ties broken randomly)."""
    rng = rng or random.Random()
    combos = list(itertools.product(*(OPTIONS[f] for f in VISUAL_FIELDS)))
    rng.shuffle(combos)
    best, best_score = None, -1
    for combo in combos:
        cand = dict(zip(VISUAL_FIELDS, combo))
        score = min((_diff(cand, p) for p in previous), default=len(VISUAL_FIELDS))
        if score > best_score:
            best, best_score = cand, score
        if best_score == len(VISUAL_FIELDS):
            break
    recipe = {**base, **best}
    recipe["version"] = max((p.get("version", 1) for p in previous), default=0) + 1
    recipe["zoom_period"] = rng.choice([4.0, 5.0, 7.0, 8.0])
    recipe["zoom_amount"] = rng.choice([0.08, 0.12, 0.15])
    recipe["outro_cta"] = not all(p.get("outro_cta") for p in previous) or rng.random() < 0.5
    return recipe


def is_meaningfully_different(new: dict, previous: list[dict]) -> bool:
    for p in previous:
        if _diff(new, p) < MIN_DIFF:
            return False
        if (new.get("hook_text") or "").strip().lower() == (p.get("hook_text") or "").strip().lower():
            return False
    return True


def _lineage(conn, root_id: int):
    return conn.execute("SELECT * FROM clips WHERE id=? OR parent_clip_id=? ORDER BY version", (root_id, root_id)).fetchall()


def _lineage_views(conn, root_id: int, first_only: bool = False) -> int | None:
    q = """SELECT MAX(pf.views) AS v FROM performance pf JOIN posts p ON p.id = pf.post_id
           JOIN clips c ON c.id = p.clip_id WHERE """ + ("c.id=?" if first_only else "(c.id=? OR c.parent_clip_id=?)")
    args = (root_id,) if first_only else (root_id, root_id)
    row = conn.execute(q, args).fetchone()
    return row["v"] if row and row["v"] is not None else None


def find_candidates(conn, cfg: Config, now: datetime | None = None) -> list[dict]:
    now = now or db.utcnow()
    interval = timedelta(days=float(cfg.get("reedit.interval_days", 90)))
    roots = conn.execute(
        """SELECT DISTINCT COALESCE(c.parent_clip_id, c.id) AS root FROM clips c
           JOIN posts p ON p.clip_id = c.id WHERE p.status='posted'""").fetchall()
    first_run = {r["root"]: _lineage_views(conn, r["root"], first_only=True) for r in roots}
    known = [v for v in first_run.values() if v is not None]
    median = statistics.median(known) if len(known) >= 5 else None
    severe = float(cfg.get("reedit.severe_underperform_ratio", 0.5))

    out = []
    for r in roots:
        root_id = r["root"]
        lineage = _lineage(conn, root_id)
        if any(c["status"] in ("pending", "review", "approved", "dry_run") for c in lineage):
            continue  # a version is already in the queue
        if any(c["status"] == "scheduled" for c in lineage):
            continue
        root = next((c for c in lineage if c["id"] == root_id), None)
        if root is None or (root["note"] or "").startswith("no_repost"):
            continue
        last_post = conn.execute(
            f"""SELECT MAX(posted_at) AS t FROM posts WHERE status='posted'
                AND clip_id IN ({','.join('?' * len(lineage))})""", [c["id"] for c in lineage]).fetchone()["t"]
        if not last_post or now - db.parse_iso(last_post) < interval:
            continue
        views = first_run.get(root_id)
        force = (root["note"] or "").startswith("force_repost")
        if median is not None and views is not None and not force:
            if views < median * severe:
                log.info("Skipping clip %s: first run %s views vs median %s", root_id, views, median)
                continue
            if cfg.get("reedit.skip_below_median", True) and views < median:
                continue
        out.append({"root_id": root_id, "views": views, "score": root["score"], "lineage": lineage})
    # Best performers first; without data, highest original score first.
    out.sort(key=lambda c: (c["views"] is not None, c["views"] or 0, c["score"]), reverse=True)
    return out


def rewrite_copy(llm, cfg: Config, text: str, previous: list[dict]) -> dict:
    prior = "\n".join(f"- hook: {p.get('hook')!r} | overlay: {p.get('hook_overlay')!r} | title: {p.get('title')!r}"
                      for p in previous)
    system = (f"You write short-form video copy for {cfg.get('brand.name')}. This clip already ran before; "
              "write fresh packaging so the repost feels new. Never reuse an earlier hook, overlay, or title.")
    user = f"""Clip transcript:
<transcript>
{text}
</transcript>

Earlier versions used:
{prior}

Write new copy. Keep start_idx=0 and end_idx=0 (boundaries are not changing) and give your honest 0-99 score
for the clip itself. hook_overlay is at most 7 words of on-screen text for the first 2 seconds; take a
different angle from the earlier ones (a question instead of a statement, a number, a bold claim, etc.).
Per-platform copy follows the usual rules: TikTok short + hashtags, YouTube searchable title under 90
characters + #shorts, Instagram 2-4 sentences ending in a question, Facebook conversational, X under 240 chars."""
    return llm(system, user, REFINE_SCHEMA)


def _weekly_reposts(conn, now: datetime) -> int:
    return conn.execute("SELECT COUNT(*) FROM clips WHERE parent_clip_id IS NOT NULL AND created_at >= ?",
                        (db.iso(now - timedelta(days=7)),)).fetchone()[0]


def run_reedit(cfg: Config, conn, llm=None, now: datetime | None = None, rng: random.Random | None = None) -> list[int]:
    now = now or db.utcnow()
    budget = int(cfg.get("reedit.max_reposts_per_week", 4)) - _weekly_reposts(conn, now)
    if budget <= 0:
        log.info("Re-edit: weekly repost cap reached")
        return []
    candidates = find_candidates(conn, cfg, now)[:budget]
    if not candidates:
        return []
    if llm is None:
        from .llm import ClaudeJSON
        llm = ClaudeJSON(cfg)
    created = []
    for cand in candidates:
        try:
            created.append(reedit_one(cfg, conn, cand, llm, rng))
        except Exception:
            log.exception("Re-edit of clip %s failed", cand["root_id"])
    return created


def reedit_one(cfg: Config, conn, cand: dict, llm, rng=None) -> int:
    lineage = [db.clip_dict(c) for c in cand["lineage"]]
    root = next(c for c in lineage if c["id"] == cand["root_id"])
    stream = conn.execute("SELECT * FROM streams WHERE id=?", (root["stream_id"],)).fetchone()
    source = Path(stream["file_path"])
    if not source.exists():
        raise FileNotFoundError(f"Source recording missing: {source}")
    transcript = json.loads((cfg.subdir("transcripts") / f"{stream['hash']}.json").read_text(encoding="utf-8"))
    words = clip_words(transcript["words"], root["start"], root["end"])
    text = " ".join(w["w"] for w in words)

    previous_recipes = [c["recipe"] for c in lineage if c.get("recipe")]
    previous_copy = [{"hook": c["hook"], "hook_overlay": (c.get("recipe") or {}).get("hook_text"), "title": c["title"]}
                     for c in lineage]
    copy = rewrite_copy(llm, cfg, text, previous_copy)
    base = default_recipe(cfg, root)
    recipe = vary_recipe(previous_recipes, base, rng)
    recipe["hook_text"] = copy.get("hook_overlay") or copy.get("hook") or ""
    if not is_meaningfully_different(recipe, previous_recipes):
        # Visual change is guaranteed by vary_recipe; this only trips if the model repeated an old hook.
        recipe["hook_text"] = f"{recipe['hook_text']} (part {recipe['version']})".strip()

    version = max(c["version"] for c in lineage) + 1
    clip = {
        "start": root["start"], "end": root["end"], "score": int(root["score"]),
        "hook": copy.get("hook") or root["hook"], "reason": f"90-day re-edit of clip {root['id']}",
        "title": copy.get("title"), "caption": copy.get("caption"), "hashtags": copy.get("hashtags"),
        "platform_captions": {p: copy[p] for p in PLATFORMS if p in copy},
    }
    clip_id = db.insert_clip(conn, root["stream_id"], clip, "pending", recipe, parent_clip_id=root["id"], version=version)
    out = cfg.subdir("clips") / f"stream_{root['stream_id']:04d}" / f"clip_{root['id']:05d}_v{version}.mp4"
    render_clip(cfg, source, clip, words, recipe, out)
    decision = gate(clip["score"], cfg)
    status = "review" if decision != "auto" else ("dry_run" if cfg.dry_run else "approved")
    db.update_clip(conn, clip_id, status=status, file_path=str(out))
    db.log_event(conn, "clip_reedited", clip_id, {"root": root["id"], "version": version, "recipe": recipe})
    log.info("Re-edit v%d of clip %s -> clip %s (%s, %s captions, %s crop, %s zoom)", version, root["id"], clip_id,
             status, recipe["caption_style"], recipe["crop_mode"], recipe["zoom"])
    if status == "approved":
        plan_posts(conn, cfg, clip_id)
    return clip_id
