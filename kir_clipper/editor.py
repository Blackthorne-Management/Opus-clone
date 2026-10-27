"""Edit a video: any raw take (phone video, a recorded reel), or a set of photos for stop motion.

A video goes through the same edit as a stream clip: transcribe, tight jump cuts (pauses, ums, retakes),
reframe to 9:16 (following your face), word-by-word captions, the hook on screen, optional stop motion,
and audio mastered to -14 LUFS. Claude can write the hook, title, captions and hashtags (scored and
cleaned like every other clip), or you write them yourself.

Photos become a stop-motion video (one photo per drawing), optionally over a sound or voice file.

The result is a normal clip waiting in **Needs review**: re-render it there with other settings, then
Approve & schedule and it follows the same posting plan as everything else. Uploads never post on
their own.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from pathlib import Path

from . import db, media
from .config import Config, secret
from .highlights import PLATFORMS, REFINE_SCHEMA, build_sentences
from .render import CROP_MODES, default_recipe, edit_summary, render_clip
from .stopmotion import DRAWING_RATES, LOOKS, from_photos, normalize_settings
from .virality import WRITING_RULES, polish

log = logging.getLogger("kir.editor")

VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")
_worker = threading.Lock()


def clean_options(raw: dict, kind: str) -> dict:
    """Validate the form options; unknown or bad values fall back to defaults."""
    from .captions import CAPTION_STYLES
    o = {
        "pace": raw.get("pace") if raw.get("pace") in ("tight", "normal", "loose", "off") else "tight",
        "caption_style": raw.get("caption_style") if raw.get("caption_style") in CAPTION_STYLES else None,
        "crop_mode": raw.get("crop_mode") if raw.get("crop_mode") in CROP_MODES else None,
        "stopmotion": normalize_settings(raw.get("stopmotion")),
        "write_copy": bool(raw.get("write_copy", True)),
        "hook_text": str(raw.get("hook_text") or "").strip()[:80],
        "title": str(raw.get("title") or "").strip()[:200],
        "caption": str(raw.get("caption") or "").strip()[:2200],
    }
    if kind == "photos":
        fps = int(raw.get("fps") or 12)
        o.update({"fps": fps if fps in DRAWING_RATES else 12,
                  "hold": max(1, min(8, int(raw.get("hold") or 2))),
                  "loop": max(1, min(10, int(raw.get("loop") or 1))),
                  "pingpong": bool(raw.get("pingpong")),
                  "look": raw.get("look") if raw.get("look") in LOOKS else "classic",
                  "fit": "contain" if raw.get("fit") == "contain" else "cover"})
    return o


def create(cfg: Config, conn, files: list[Path], options: dict, audio: Path | None = None) -> int:
    """Store the uploads and queue the edit. Returns the edit id."""
    files = [Path(f) for f in files]
    videos = [f for f in files if f.suffix.lower() in VIDEO_EXTS]
    photos = [f for f in files if f.suffix.lower() in IMAGE_EXTS]
    if videos and photos:
        raise ValueError("Upload one video, or a set of photos, not both")
    if len(videos) > 1:
        raise ValueError("Upload one video at a time")
    if not videos and not photos:
        raise ValueError("Choose a video (mp4, mov...) or photos (jpg, png...)")
    if audio is not None and Path(audio).suffix.lower() not in AUDIO_EXTS + VIDEO_EXTS:
        raise ValueError("The sound file must be mp3, wav, m4a, aac, flac or ogg")
    kind = "video" if videos else "photos"
    opts = clean_options(options, kind)
    now = db.iso(db.utcnow())
    name = videos[0].name if videos else f"{len(photos)} photos"
    edit_id = conn.execute("INSERT INTO edits(kind, name, options, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                           (kind, name, json.dumps(opts), "queued", now, now)).lastrowid
    folder = cfg.subdir("edits") / f"edit_{edit_id:04d}"
    folder.mkdir(parents=True, exist_ok=True)
    stored = []
    for f in (videos or photos):
        dest = folder / ("source" + f.suffix.lower() if videos else f.name)
        shutil.move(str(f), dest)
        stored.append(dest.name)
    if audio is not None:
        dest = folder / ("sound" + Path(audio).suffix.lower())
        shutil.move(str(audio), dest)
        opts["sound"] = dest.name
    opts["files"] = stored
    db.update_edit(conn, edit_id, options=opts)
    db.log_event(conn, "edit_queued", edit_id, {"kind": kind, "name": name})
    return edit_id


# --------------------------------------------------------------------------- processing

def write_copy(cfg: Config, llm, transcript_text: str, duration: float) -> dict:
    system = (f"You package short-form videos for {cfg.get('brand.name', 'the creator')} "
              "(TikTok, Instagram Reels, YouTube Shorts). The video is already edited; you write what goes around it.")
    user = f"""Transcript of the edited video ({duration:.0f} seconds):
<transcript>
{transcript_text}
</transcript>

Keep start_idx=0 and end_idx=0. score: your honest 0-99 for how well this works as a short (hook, payoff, pace).
hook: the opening line as spoken. reason: one sentence on why it works (or what would make it stronger).
hook_overlay: at most 7 words of on-screen text for the first 2 seconds (no hashtags, no emojis).
hook_overlay_alt: a second on-screen hook for an A/B test, at most 7 words, a clearly different angle.
Write a short title and a general caption, 3-6 relevant hashtags (with #), and per-platform copy:
- tiktok: title = short punchy line; caption = 1-2 short sentences plus hashtags.
- youtube: title = searchable Shorts title under 90 characters; caption = 1-2 sentences + #shorts.
- instagram: 2-4 sentences, a question to drive comments, then hashtags.
- facebook: conversational 1-3 sentences. x: under 240 characters including hashtags.
Everything must be honest to what is actually said.

{WRITING_RULES}"""
    out = llm(system, user, REFINE_SCHEMA)
    out["platform_captions"] = {p: out.pop(p) for p in PLATFORMS if p in out}
    return out


def _stream_for(conn, source: Path, file_hash: str, duration: float, name: str) -> int:
    existing = db.stream_by_hash(conn, file_hash)
    if existing:
        return existing["id"]
    stream_id = db.insert_stream(conn, source, file_hash)
    db.update_stream(conn, stream_id, kind="upload", status="done", duration=duration, original_path=name,
                     processed_at=db.iso(db.utcnow()))
    return stream_id


def process(cfg: Config, conn, edit_id: int, llm=None, transcriber=None) -> int:
    """Run one queued edit. Returns the clip id."""
    row = conn.execute("SELECT * FROM edits WHERE id=?", (edit_id,)).fetchone()
    opts = json.loads(row["options"])
    folder = cfg.subdir("edits") / f"edit_{edit_id:04d}"
    db.update_edit(conn, edit_id, status="working", error=None)
    if row["kind"] == "photos":
        return _process_photos(cfg, conn, edit_id, row, opts, folder)

    source = folder / opts["files"][0]
    info = media.probe(source)
    if info["width"] == 0:
        raise RuntimeError("That file has no video picture")
    file_hash = media.fast_hash(source)
    stream_id = _stream_for(conn, source, file_hash, info["duration"], row["name"])
    words: list[dict] = []
    if info["has_audio"]:
        from .transcribe import transcribe
        words = transcribe(cfg, source, file_hash, info["duration"], backend=transcriber)["words"]
    text = " ".join(w["w"] for w in words)
    clip = {"start": 0.0, "end": round(info["duration"], 3), "score": 0, "reason": "your upload",
            "hook": (build_sentences(words)[0]["text"] if words else "")[:200],
            "title": opts["title"] or Path(row["name"]).stem, "caption": opts["caption"], "hashtags": []}
    use_claude = opts["write_copy"] and len(words) >= 8 and (llm is not None or secret("ANTHROPIC_API_KEY"))
    if use_claude:
        if llm is None:
            from .llm import ClaudeJSON
            llm = ClaudeJSON(cfg)
        copy = write_copy(cfg, llm, text, info["duration"])
        clip.update({k: copy[k] for k in ("score", "reason", "hook_overlay", "hook_overlay_alt", "hashtags",
                                          "platform_captions") if k in copy})
        clip["hook"] = copy.get("hook") or clip["hook"]
        clip["title"] = opts["title"] or copy.get("title") or clip["title"]
        clip["caption"] = opts["caption"] or copy.get("caption") or ""
    if opts["hook_text"]:
        clip["hook_overlay"] = opts["hook_text"]
    clip["checks"] = polish(clip)
    recipe = default_recipe(cfg, clip)
    recipe.update({"upload": True, "jumpcut": opts["pace"], "stopmotion": opts["stopmotion"],
                   "hook_overlay": bool(recipe["hook_text"])})
    for key in ("caption_style", "crop_mode"):
        if opts[key]:
            recipe[key] = opts[key]
    clip_id = db.insert_clip(conn, stream_id, clip, "pending", recipe)
    out = cfg.subdir("clips") / f"upload_{edit_id:04d}" / f"clip_{clip_id:05d}_v1.mp4"
    render_clip(cfg, source, clip, words, recipe, out, info)
    db.update_clip(conn, clip_id, status="review", file_path=str(out), note="uploaded",
                   checks={**clip["checks"], "edit": edit_summary(out)})
    db.update_edit(conn, edit_id, status="ready", stream_id=stream_id, clip_id=clip_id)
    db.log_event(conn, "edit_ready", edit_id, {"clip": clip_id})
    return clip_id


def _process_photos(cfg: Config, conn, edit_id: int, row, opts: dict, folder: Path) -> int:
    r = cfg.get("render") or {}
    out_dir = cfg.subdir("clips") / f"upload_{edit_id:04d}"
    out = out_dir / "stopmotion.mp4"
    sound = folder / opts["sound"] if opts.get("sound") else None
    from_photos([folder / f for f in opts["files"]], out, fps=opts["fps"], hold=opts["hold"],
                width=int(r.get("width", 1080)), height=int(r.get("height", 1920)), fit=opts["fit"],
                look=opts["look"], loop=opts["loop"], pingpong=opts["pingpong"], audio=sound, cfg=cfg)
    info = media.probe(out)
    file_hash = media.fast_hash(out)
    stream_id = _stream_for(conn, out, file_hash, info["duration"], row["name"])
    clip = {"start": 0.0, "end": round(info["duration"], 3), "score": 0, "reason": "stop motion from your photos",
            "hook": opts["hook_text"], "hook_overlay": opts["hook_text"],
            "title": opts["title"] or "Stop motion", "caption": opts["caption"], "hashtags": []}
    clip["checks"] = polish(clip)
    # Rendered already: re-edits repost this file (like an imported reel without a clean copy).
    recipe = {**default_recipe(cfg, clip), "imported": True, "photos": True, "upload": True, "has_text": False,
              "clean": False, "auto_repost": False}
    clip_id = db.insert_clip(conn, stream_id, clip, "review", recipe)
    db.update_clip(conn, clip_id, file_path=str(out), note="uploaded")
    db.update_edit(conn, edit_id, status="ready", stream_id=stream_id, clip_id=clip_id)
    db.log_event(conn, "edit_ready", edit_id, {"clip": clip_id, "photos": len(opts["files"])})
    return clip_id


def drain(cfg: Config, llm=None, transcriber=None) -> int:
    """Process every queued edit, one at a time. Returns how many ran."""
    if not _worker.acquire(blocking=False):
        return 0  # another thread is already draining the queue
    done = 0
    try:
        conn = db.connect(cfg.db_path)
        while True:
            row = conn.execute("SELECT id FROM edits WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                break
            try:
                process(cfg, conn, row["id"], llm=llm, transcriber=transcriber)
            except Exception as exc:
                log.exception("Edit %s failed", row["id"])
                msg = str(exc)
                if isinstance(exc, ImportError) or "faster_whisper" in msg:
                    msg = "Transcription isn't installed on this PC. Run scripts\\install.ps1 again."
                db.update_edit(conn, row["id"], status="failed", error=msg[:500])
            done += 1
    finally:
        _worker.release()
    return done


def resume(cfg: Config, conn) -> None:
    """After a restart: edits that were mid-render go back in the queue, scripts mid-write are marked failed."""
    conn.execute("UPDATE edits SET status='queued' WHERE status='working'")
    conn.execute("UPDATE scripts SET status='failed', error='Interrupted by a restart; write it again' WHERE status='writing'")
    if conn.execute("SELECT 1 FROM edits WHERE status='queued'").fetchone():
        kick(cfg)


def kick(cfg: Config) -> None:
    """Start draining the queue in the background (no-op if a drain is already running)."""
    threading.Thread(target=drain, args=(cfg,), name="edits", daemon=True).start()


def retry(conn, edit_id: int) -> None:
    row = conn.execute("SELECT status FROM edits WHERE id=?", (edit_id,)).fetchone()
    if not row or row["status"] != "failed":
        raise ValueError("Only failed edits can be retried")
    db.update_edit(conn, edit_id, status="queued", error=None)


def list_edits(conn, limit: int = 30) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM edits ORDER BY id DESC LIMIT ?", (limit,)):
        d = dict(r)
        d["options"] = json.loads(d["options"])
        clip = db.get_clip(conn, d["clip_id"]) if d["clip_id"] else None
        d["clip"] = {k: clip[k] for k in ("id", "status", "title", "score")} if clip else None
        if clip:
            d["clip"]["checks"] = db.clip_dict(clip).get("checks")
        out.append(d)
    return out


def safe_name(name: str) -> str:
    return re.sub(r"[^\w.\- ]+", "_", Path(name or "upload").name)[:120] or "upload"
