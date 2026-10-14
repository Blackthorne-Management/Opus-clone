"""End-to-end processing of one finished recording."""

from __future__ import annotations

import json
import logging
import shutil
from collections import Counter
from pathlib import Path

from . import db, media
from .cadence import plan_clip
from .config import Config
from .highlights import clip_words, find_highlights, fmt_ts
from .notify import notify
from .render import default_recipe, render_clip
from .scheduler import plan_posts
from .transcribe import transcribe

log = logging.getLogger("kir.pipeline")


def gate(score: int, cfg: Config) -> str:
    """'drop' below the floor, 'auto' at/above the auto-post threshold, otherwise 'review'."""
    if score < int(cfg.get("review.auto_reject_floor", 60)):
        return "drop"
    if score >= int(cfg.get("review.auto_post_threshold", 90)):
        return "auto"
    return "review"


def _unique_dest(folder: Path, name: str) -> Path:
    dest = folder / name
    if not dest.exists():
        return dest
    stamp = db.utcnow().strftime("%Y%m%d-%H%M%S")
    return folder / f"{Path(name).stem}__{stamp}{Path(name).suffix}"


def _move(src: Path, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(folder, src.name)
    shutil.move(str(src), dest)
    return dest


def score_histogram(scores: list[int]) -> str:
    buckets = Counter(min(9, s // 10) for s in scores)
    rows = []
    for b in range(9, -1, -1):
        if buckets.get(b):
            rows.append(f"  {b * 10:2d}-{b * 10 + 9:2d}: {'#' * buckets[b]} ({buckets[b]})")
    return "\n".join(rows) or "  (no clips)"


def process_file(cfg: Config, conn, path: Path, llm=None, transcriber=None, claim: bool = True) -> int | None:
    """Process one finished recording sitting in the inbox. Returns the stream id (None if duplicate).

    With claim=False the file is processed where it is (used by `reprocess`)."""
    path = Path(path)
    file_hash = media.fast_hash(path)
    existing = db.stream_by_hash(conn, file_hash)
    if existing and existing["status"] == "done":
        dest = _move(path, cfg.path("processed") / "duplicates")
        log.info("Duplicate of stream %s; moved to %s without processing", existing["id"], dest)
        db.log_event(conn, "duplicate_skipped", existing["id"], str(dest))
        return None

    # Claim the file: move it out of the inbox before any slow work, so it is never picked up twice.
    claimed = _move(path, cfg.path("processed")) if claim else path
    if existing:
        stream_id = existing["id"]
        db.update_stream(conn, stream_id, file_path=str(claimed), status="processing", error=None)
        # Clear leftovers from an interrupted run (never touches clips that were approved or posted).
        conn.execute("""DELETE FROM clips WHERE stream_id=? AND kind='short' AND status IN ('pending','failed','dropped','review','dry_run')
                        AND id NOT IN (SELECT clip_id FROM posts)""", (stream_id,))
    else:
        stream_id = db.insert_stream(conn, claimed, file_hash)
        db.update_stream(conn, stream_id, original_path=str(path), file_path=str(claimed))
    db.log_event(conn, "stream_claimed", stream_id, {"from": str(path), "to": str(claimed)})
    log.info("Stream %s: processing %s", stream_id, claimed.name)

    try:
        source = claimed
        if claimed.stat().st_size == 0:
            raise RuntimeError("The file is empty (0 bytes): the recording or export never finished. "
                               "Use the original OBS recording, or re-export and let it finish.")
        ext = claimed.suffix.lower()
        if ext == ".flv" or (ext == ".mkv" and cfg.get("watcher.remux_mkv", False)):
            mp4 = _unique_dest(claimed.parent, claimed.stem + ".mp4")
            log.info("Remuxing %s -> %s", claimed.name, mp4.name)
            source = media.remux_to_mp4(claimed, mp4)
            db.update_stream(conn, stream_id, file_path=str(source))
        try:
            info = media.probe(source)
        except RuntimeError as exc:
            if "moov atom not found" in str(exc):
                raise RuntimeError("This MP4 is incomplete (moov atom not found): the recording or export was cut off "
                                   "before it finished. Use the original OBS recording, or re-export it.") from exc
            raise
        db.update_stream(conn, stream_id, duration=info["duration"])
        if not info["has_audio"]:
            raise RuntimeError("Recording has no audio track")

        transcript = transcribe(cfg, source, file_hash, info["duration"], backend=transcriber)
        if llm is None:
            from .llm import ClaudeJSON
            llm = ClaudeJSON(cfg)
        from .hooktests import lessons
        clips = find_highlights(cfg, transcript, llm, lessons=lessons(conn, cfg))
        created = create_clips(cfg, conn, stream_id, source, clips, transcript["words"], info)
        try:
            from .longform import prepare_segments
            prepare_segments(cfg, conn, stream_id, transcript, info["duration"])
        except Exception:
            log.exception("Could not prepare the long-form edit for stream %s", stream_id)

        db.update_stream(conn, stream_id, status="done", processed_at=db.iso(db.utcnow()))
        db.log_event(conn, "stream_done", stream_id, created)
        scores = [c["score"] for c in clips]
        notify(cfg, f"Stream processed: {claimed.name}",
               f"{len(clips)} clips found ({info['duration'] / 60:.0f} min stream).\n"
               f"Auto-post: {created['auto']}  Review: {created['review']}  Dropped: {created['drop']}  "
               f"Render failures: {created['failed']}\nScore distribution:\n{score_histogram(scores)}"
               + ("\nDRY RUN: nothing will be posted. Check whether the 90+ count looks right." if cfg.dry_run else ""))
        return stream_id
    except Exception as exc:
        log.exception("Stream %s failed", stream_id)
        try:
            claimed = _move(claimed, cfg.path("failed"))
        except OSError:
            log.warning("Could not move %s to the failed folder", claimed)
        db.update_stream(conn, stream_id, status="failed", error=str(exc)[:1000], file_path=str(claimed))
        db.log_event(conn, "stream_failed", stream_id, str(exc)[:1000])
        notify(cfg, f"Processing failed: {claimed.name}", f"{exc}\nThe recording is untouched at "
               f"{claimed}. Fix the issue and run `kir reprocess {stream_id}`.", level="alert")
        return stream_id


def create_clips(cfg: Config, conn, stream_id: int, source: Path, clips: list[dict], words: list[dict],
                 info: dict) -> dict:
    counts = {"auto": 0, "review": 0, "drop": 0, "failed": 0}
    out_dir = cfg.subdir("clips") / f"stream_{stream_id:04d}"
    auto_ids = []
    for clip in sorted(clips, key=lambda c: -c["score"]):
        decision = gate(clip["score"], cfg)
        counts[decision] += 1
        recipe = default_recipe(cfg, clip)
        status = {"drop": "dropped", "review": "review", "auto": "dry_run" if cfg.dry_run else "approved"}[decision]
        clip_id = db.insert_clip(conn, stream_id, clip, "pending", recipe)
        log.info("Clip %s %s-%s score %d -> %s | %s", clip_id, fmt_ts(clip["start"]), fmt_ts(clip["end"]),
                 clip["score"], status, clip.get("hook", "")[:70])
        if decision == "drop" and not cfg.get("review.render_rejected", False):
            db.update_clip(conn, clip_id, status="dropped", note=f"below floor {cfg.get('review.auto_reject_floor')}")
            db.log_event(conn, "clip_dropped", clip_id, {"score": clip["score"], "reason": clip.get("reason")})
            continue
        out = out_dir / f"clip_{clip_id:05d}_v1.mp4"
        try:
            render_clip(cfg, source, clip, clip_words(words, clip["start"], clip["end"]), recipe, out, info)
        except Exception as exc:
            log.exception("Render failed for clip %s", clip_id)
            db.update_clip(conn, clip_id, status="failed", note=f"render: {str(exc)[:500]}")
            counts["failed"] += 1
            continue
        db.update_clip(conn, clip_id, status=status, file_path=str(out))
        db.log_event(conn, "clip_created", clip_id, {"score": clip["score"], "status": status})
        if status == "approved":
            auto_ids.append(clip_id)
    for clip_id in auto_ids:  # already sorted best-first, so the best clip gets the earliest slot
        plan_clip(cfg, conn, clip_id)
    return counts


def approve_clip(cfg: Config, conn, clip_id: int, platforms: list[str] | None = None) -> None:
    clip = db.get_clip(conn, clip_id)
    if clip is None or not clip["file_path"]:
        raise ValueError(f"Clip {clip_id} has no rendered file")
    if platforms is None and clip["kind"] == "longform":
        platforms = list(cfg.get("longform.platforms") or ["youtube"])
    db.update_clip(conn, clip_id, status="approved")
    db.log_event(conn, "clip_approved", clip_id)
    if platforms is None and clip["kind"] == "short":
        plan_clip(cfg, conn, clip_id)
    else:
        plan_posts(conn, cfg, clip_id, platforms=platforms)


def reject_clip(conn, clip_id: int, note: str | None = None) -> None:
    db.update_clip(conn, clip_id, status="rejected", note=note)
    conn.execute("UPDATE posts SET status='cancelled' WHERE clip_id=? AND status='planned'", (clip_id,))
    db.log_event(conn, "clip_rejected", clip_id, note)


def edit_clip(cfg: Config, conn, clip_id: int, changes: dict) -> None:
    """Update copy and/or re-render with recipe or boundary changes from the review UI."""
    row = db.get_clip(conn, clip_id)
    clip = db.clip_dict(row)
    text_fields = {k: changes[k] for k in ("title", "caption", "hook", "hashtags", "platform_captions") if k in changes}
    recipe = dict(clip["recipe"] or default_recipe(cfg, clip))
    recipe_changes = changes.get("recipe") or {}
    bounds = {k: float(changes[k]) for k in ("start", "end") if changes.get(k) not in (None, "")}
    needs_render = bool(recipe_changes) or any(abs(bounds[k] - clip[k]) > 0.01 for k in bounds)
    if text_fields:
        db.update_clip(conn, clip_id, **text_fields)
    if not needs_render:
        db.log_event(conn, "clip_edited", clip_id, list(text_fields))
        return
    recipe.update(recipe_changes)
    clip.update(bounds)
    stream = conn.execute("SELECT * FROM streams WHERE id=?", (clip["stream_id"],)).fetchone()
    transcript = json.loads((cfg.subdir("transcripts") / f"{stream['hash']}.json").read_text(encoding="utf-8"))
    old = Path(clip["file_path"]) if clip["file_path"] else None
    edits = 1 + conn.execute("SELECT COUNT(*) FROM events WHERE kind='clip_rerendered' AND ref=?",
                             (str(clip_id),)).fetchone()[0]
    out = cfg.subdir("clips") / f"stream_{clip['stream_id']:04d}" / f"clip_{clip_id:05d}_v{clip['version']}_e{edits}.mp4"
    render_clip(cfg, Path(stream["file_path"]), clip, clip_words(transcript["words"], clip["start"], clip["end"]),
                recipe, out)
    db.update_clip(conn, clip_id, recipe_json=recipe, file_path=str(out), start=clip["start"], end=clip["end"])
    db.log_event(conn, "clip_rerendered", clip_id, {"recipe": recipe_changes, "bounds": bounds,
                                                     "previous_file": str(old) if old else None})


def reprocess(cfg: Config, conn, stream_id: int, llm=None, transcriber=None) -> None:
    """Re-run a failed stream from wherever its file now lives."""
    row = conn.execute("SELECT * FROM streams WHERE id=?", (stream_id,)).fetchone()
    if row is None:
        raise ValueError(f"No stream {stream_id}")
    src = Path(row["file_path"])
    if not src.exists():
        raise FileNotFoundError(src)
    if src.parent.resolve() != cfg.path("processed").resolve():
        src = _move(src, cfg.path("processed"))
    db.update_stream(conn, stream_id, status="failed", file_path=str(src))
    process_file(cfg, conn, src, llm=llm, transcriber=transcriber, claim=False)
