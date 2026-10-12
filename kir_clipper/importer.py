"""Import a reel you already posted, so it joins the 'post everything five times' schedule.

You say when it was first posted (day 1) and which steps you've already done. Everything else is
scheduled from that date:
  - steps already done are recorded as posted (so the countdowns and 90-day reposts line up),
  - steps still in the future go on the calendar,
  - steps whose date has already passed are skipped, or posted as soon as possible if you choose.
Reposts at +90/+180 days are made by the normal re-edit job when they come due.

A finished reel usually has captions burned in. Flipping it would make the text read backwards, and
re-editing it would stack captions on captions. So:
  - with a clean copy (no on-screen text) the app transcribes it and makes a proper mirrored version and
    fresh re-edits with new captions;
  - without one, the mirrored step is skipped if the reel has on-screen text (a reel without text is just
    flipped), and the +90/+180 reposts reuse the file you posted.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from . import db, media
from .cadence import _local_midnight, render_variant, schedule_step
from .config import Config
from .render import default_recipe
from .scheduler import _tz
from .transcribe import transcribe

log = logging.getLogger("kir.importer")


def cadence_steps(cfg: Config) -> list[dict]:
    """All five steps with their day offsets, including the reposts that the re-edit job makes."""
    steps = [dict(s) for s in (cfg.get("cadence.steps") or [])]
    interval = int(float(cfg.get("reedit.interval_days", 90)))
    for n in range(1, int(cfg.get("reedit.max_reposts", 2)) + 1):
        steps.append({"name": f"repost_{n}", "day": interval * n,
                      "platforms": list(cfg.get("reedit.platforms") or cfg.get("publishing.platforms") or [])})
    return steps


def mirror_file(src: Path, dst: Path, cfg: Config) -> Path:
    r = cfg.get("render") or {}
    enc = media.pick_encoder(r.get("encoder", "auto"))
    media.ffmpeg("-i", src, "-vf", "hflip", *media.encoder_args(enc, int(r.get("crf", 20)), int(r.get("nvenc_cq", 23))),
                 "-c:a", "copy", "-movflags", "+faststart", dst)
    return dst


def _store(src: Path, folder: Path, file_hash: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{file_hash[:12]}_{src.name}"
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    return dest


def import_reel(cfg: Config, conn, video: Path, posted_at: datetime, *, title: str = "", caption: str = "",
                platforms: list[str] | None = None, done_steps: list[str] | None = None, has_text: bool = True,
                clean: Path | None = None, missed: str = "skip", auto_repost: bool = True,
                transcriber=None, now: datetime | None = None) -> dict:
    """Returns {"clip_id", "steps": [{name, label, action, at}]} describing what happened to each step."""
    from .cadence import step_label

    now = now or db.utcnow()
    video = Path(video)
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=_tz(cfg))
    if posted_at > now + timedelta(minutes=5):
        raise ValueError("The first post date is in the future. Use this form for reels you've already posted.")
    platforms = platforms or ["instagram"]
    done = set(done_steps or ["feed"])
    video_hash = media.fast_hash(video)
    if conn.execute("SELECT 1 FROM events WHERE kind='reel_imported' AND detail LIKE ?",
                    (f'%"{video_hash}"%',)).fetchone():
        raise ValueError("This reel has already been imported")

    folder = cfg.subdir("imports")
    posted_file = _store(video, folder, video_hash)
    info = media.probe(posted_file)
    source, source_hash, source_info = posted_file, video_hash, info
    if clean:
        clean_hash = media.fast_hash(Path(clean))
        source = _store(Path(clean), folder, clean_hash)
        source_hash, source_info = clean_hash, media.probe(source)
    if db.stream_by_hash(conn, source_hash):
        raise ValueError("This reel has already been imported")

    stream_id = db.insert_stream(conn, source, source_hash)
    db.update_stream(conn, stream_id, kind="import", status="done", duration=source_info["duration"],
                     original_path=str(video), processed_at=db.iso(now))
    if clean:
        transcribe(cfg, source, source_hash, source_info["duration"], backend=transcriber)

    clip = {"start": 0.0, "end": round(source_info["duration"], 3), "score": 0, "hook": title or video.stem,
            "reason": "imported reel", "title": title or video.stem, "caption": caption, "hashtags": []}
    recipe = {**default_recipe(cfg, clip), "imported": True, "has_text": has_text, "clean": bool(clean),
              "auto_repost": auto_repost}
    clip_id = db.insert_clip(conn, stream_id, clip, "posted", recipe)
    db.update_clip(conn, clip_id, file_path=str(posted_file), note="imported")
    db.log_event(conn, "reel_imported", clip_id, {"hash": video_hash, "posted_at": db.iso(posted_at),
                                                   "done": sorted(done), "clean": bool(clean)})

    tz = _tz(cfg)
    report = []
    clip_row = db.clip_dict(db.get_clip(conn, clip_id))
    trial_mode = cfg.get("cadence.trial_mode", "manual")
    for step in cadence_steps(cfg):
        name, day, trial = step["name"], int(step.get("day", 0)), bool(step.get("trial"))
        label = step_label(name, cfg)
        due = posted_at if day == 0 else _local_midnight(posted_at, tz, day).replace(
            hour=posted_at.astimezone(tz).hour, minute=posted_at.astimezone(tz).minute)
        step_platforms = platforms if name == "feed" else (step.get("platforms") or platforms)
        entry = {"name": name, "label": label, "due": db.iso(due)}
        if name in done:
            at = min(due, now)
            target = clip_id
            if name.startswith("repost_"):  # an earlier repost you already did: record it as its own version
                n = int(name.split("_")[1])
                target = db.insert_clip(conn, stream_id, {**clip, "reason": f"repost {n} (imported)"}, "posted",
                                        recipe, parent_clip_id=clip_id, version=n + 1)
                db.update_clip(conn, target, file_path=str(posted_file), note="imported")
            for platform in step_platforms:
                pid = db.insert_post(conn, target, platform, at, step=name, trial=trial)
                db.update_post(conn, pid, status="posted", posted_at=db.iso(at))
            report.append({**entry, "action": "already posted", "at": db.iso(at)})
            continue
        if name.startswith("repost_"):
            report.append({**entry, "action": "repost job", "at": db.iso(due)})
            continue  # the 90-day re-edit job makes these when they come due
        if trial and trial_mode == "skip":
            report.append({**entry, "action": "skipped (trial reels are off)"})
            continue
        day_over = _local_midnight(due, tz, 1) if day else due + timedelta(hours=24)
        if day_over <= now and missed != "now":
            report.append({**entry, "action": "skipped (date passed)"})
            continue
        file_path = None
        if step.get("mirror"):
            try:
                if clean:
                    file_path = str(render_variant(cfg, conn, clip_row, {"mirror": True}, "mirror"))
                elif not has_text:
                    file_path = str(mirror_file(posted_file, posted_file.with_name(posted_file.stem + "_mirror.mp4"), cfg))
                else:
                    report.append({**entry, "action": "skipped (on-screen text would read backwards; "
                                                      "import a clean copy to enable)"})
                    continue
            except Exception as exc:
                log.exception("Mirrored version failed for imported clip %s", clip_id)
                report.append({**entry, "action": f"skipped (mirror failed: {exc})"})
                continue
        not_before = None if day_over <= now else (due if day == 0 else _local_midnight(posted_at, tz, day))
        made = schedule_step(cfg, conn, clip_id, name, step_platforms, now, not_before, trial, file_path)
        if made:
            report.append({**entry, "action": "scheduled", "at": db.iso(min(t for _, t in made))})
    db.log_event(conn, "import_planned", clip_id, report)
    log.info("Imported reel %s as clip %s: %s", video.name, clip_id, [(r["name"], r["action"]) for r in report])
    return {"clip_id": clip_id, "steps": report}
