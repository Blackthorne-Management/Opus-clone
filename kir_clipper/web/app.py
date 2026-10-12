"""Local review dashboard: http://127.0.0.1:8765"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from datetime import datetime

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from .. import db
from ..longform import apply_edits, set_segments
from ..config import Config
from ..performance import score_report
from ..pipeline import approve_clip, edit_clip, reject_clip
from ..cadence import step_label, timeline
from ..importer import cadence_steps, import_reel
from ..scheduler import copy_for, mark_posted, next_slot

log = logging.getLogger("kir.web")
HERE = Path(__file__).parent


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="KIR Clipper", docs_url=None, redoc_url=None)

    def conn():
        return db.connect(cfg.db_path)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (HERE / "index.html").read_text(encoding="utf-8")

    @app.get("/api/summary")
    def summary():
        c = conn()
        counts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM clips WHERE kind='short' GROUP BY status")}
        scores = [r["score"] for r in c.execute("SELECT score FROM clips WHERE parent_clip_id IS NULL AND kind='short'")]
        posts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM posts GROUP BY status")}
        streams = [dict(r) for r in c.execute(
            "SELECT id, original_path, duration, status, error, processed_at FROM streams ORDER BY id DESC LIMIT 10")]
        return {
            "dry_run": cfg.dry_run,
            "auto_post_threshold": cfg.get("review.auto_post_threshold"),
            "auto_reject_floor": cfg.get("review.auto_reject_floor"),
            "platforms": cfg.get("publishing.platforms"),
            "timezone": cfg.get("publishing.upload_post.timezone") or "UTC",
            "clips": counts, "posts": posts, "streams": streams,
            "histogram": [sum(1 for s in scores if b * 10 <= s < b * 10 + 10) for b in range(10)],
            "performance": score_report(c),
            "manual_due": c.execute("SELECT COUNT(*) FROM posts WHERE status='manual'").fetchone()[0],
            "cadence": [{"name": st["name"], "label": step_label(st["name"], cfg), "day": int(st.get("day", 0)),
                         "trial": bool(st.get("trial")), "mirror": bool(st.get("mirror"))} for st in cadence_steps(cfg)]
                       if cfg.get("cadence.enabled") else [],
        }

    @app.get("/api/clips")
    def clips(status: str = "review", limit: int = 100):
        c = conn()
        statuses = status.split(",")
        rows = db.clips_with_status(c, statuses)[:limit]
        out = []
        platforms = cfg.get("publishing.platforms") or []
        for r in rows:
            d = db.clip_dict(r)
            d["posts"] = [dict(p) for p in c.execute("SELECT * FROM posts WHERE clip_id=? ORDER BY publish_at", (r["id"],))]
            d["copy"] = {p: dict(zip(("title", "caption"), copy_for(d, p))) for p in platforms}
            if r["status"] == "review" and platforms:
                d["suggested_time"] = db.iso(next_slot(c, cfg, platforms[0], db.utcnow()))
            d["duration"] = round(d["end"] - d["start"], 1)
            d["timeline"] = timeline(cfg, c, r["id"]) if d["posts"] else []
            out.append(d)
        return out

    @app.get("/media/clip/{clip_id}")
    def clip_media(clip_id: int):
        row = db.get_clip(conn(), clip_id)
        if not row or not row["file_path"] or not Path(row["file_path"]).exists():
            raise HTTPException(404)
        return FileResponse(row["file_path"], media_type="video/mp4")

    @app.post("/api/clips/{clip_id}/approve")
    def approve(clip_id: int, body: dict = Body(default={})):
        try:
            approve_clip(cfg, conn(), clip_id, platforms=body.get("platforms") or None)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}

    @app.post("/api/clips/{clip_id}/reject")
    def reject(clip_id: int, body: dict = Body(default={})):
        reject_clip(conn(), clip_id, body.get("note"))
        return {"ok": True}

    @app.post("/api/clips/{clip_id}/edit")
    def edit(clip_id: int, body: dict = Body(...)):
        try:
            edit_clip(cfg, conn(), clip_id, body)
        except Exception as exc:
            log.exception("Edit failed")
            raise HTTPException(400, str(exc))
        return {"ok": True}

    @app.post("/api/clips/{clip_id}/flag")
    def flag(clip_id: int, body: dict = Body(...)):
        note = body.get("note")
        if note not in ("no_repost", "force_repost", None):
            raise HTTPException(400, "note must be no_repost or force_repost")
        c = conn()
        db.update_clip(c, clip_id, note=note)
        db.log_event(c, "clip_flagged", clip_id, note)
        return {"ok": True}

    @app.get("/api/posts")
    def posts(limit: int = 200):
        rows = conn().execute(
            """SELECT p.*, c.title, c.score, c.kind, COALESCE(p.file_path, c.file_path) AS media_path
               FROM posts p JOIN clips c ON c.id=p.clip_id
               ORDER BY CASE WHEN p.status='manual' THEN -1 WHEN p.status IN ('planned','submitted') THEN 0 ELSE 1 END,
                        CASE WHEN p.status IN ('planned','submitted') THEN p.publish_at END ASC,
                        p.publish_at DESC LIMIT ?""",
            (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["label"] = step_label(d["step"], cfg) if d["kind"] == "short" else "Long-form video"
            out.append(d)
        return out

    @app.post("/api/posts/{post_id}/mark-posted")
    def post_mark_posted(post_id: int, body: dict = Body(default={})):
        try:
            mark_posted(conn(), post_id, body.get("url"))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}

    @app.get("/media/post/{post_id}")
    def post_media(post_id: int):
        row = conn().execute("""SELECT COALESCE(p.file_path, c.file_path) AS f FROM posts p
                                JOIN clips c ON c.id=p.clip_id WHERE p.id=?""", (post_id,)).fetchone()
        if not row or not row["f"] or not Path(row["f"]).exists():
            raise HTTPException(404)
        return FileResponse(row["f"], media_type="video/mp4", filename=Path(row["f"]).name)

    @app.post("/api/posts/{post_id}/cancel")
    def cancel(post_id: int):
        c = conn()
        row = c.execute("SELECT status FROM posts WHERE id=?", (post_id,)).fetchone()
        if not row or row["status"] not in ("planned", "manual"):
            raise HTTPException(400, "Only planned posts can be cancelled here; submitted posts must be removed in Upload-Post")
        db.update_post(c, post_id, status="cancelled")
        db.log_event(c, "post_cancelled", post_id)
        return {"ok": True}

    @app.get("/api/longform")
    def longform_list():
        c = conn()
        out = []
        for st in c.execute("""SELECT DISTINCT s.id, s.original_path, s.duration FROM streams s
                               JOIN longform l ON l.stream_id = s.id ORDER BY s.id DESC LIMIT 20"""):
            segs = []
            for r in c.execute("SELECT * FROM longform WHERE stream_id=? ORDER BY idx", (st["id"],)):
                d = dict(r)
                plan = json.loads(d.pop("plan_json") or "null") or {}
                d["cuts"] = plan.get("cuts", [])
                d["popups"] = plan.get("popups", [])
                d["meta"] = plan.get("meta", {})
                d["render"] = plan.get("render")
                d["clip"] = dict(db.get_clip(c, r["clip_id"])) if r["clip_id"] else None
                segs.append(d)
            out.append({"stream": {"id": st["id"], "name": Path(st["original_path"]).name, "duration": st["duration"]},
                        "segments": segs})
        return out

    @app.post("/api/streams/{stream_id}/segments")
    def longform_segments(stream_id: int, body: dict = Body(...)):
        c = conn()
        st = c.execute("SELECT duration FROM streams WHERE id=?", (stream_id,)).fetchone()
        if not st:
            raise HTTPException(404)
        try:
            ids = set_segments(c, stream_id, body.get("segments") or [], st["duration"])
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "ids": ids}

    @app.post("/api/longform/{lf_id}/edit")
    def longform_edit(lf_id: int, body: dict = Body(...)):
        try:
            queued = apply_edits(cfg, conn(), lf_id, body)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "rerender": queued}

    @app.post("/api/import")
    def import_upload(video: UploadFile = File(...), clean: UploadFile | None = File(None),
                      posted_at: str = Form(...), title: str = Form(""), caption: str = Form(""),
                      platforms: str = Form("instagram"), done: str = Form("feed"), has_text: bool = Form(True),
                      missed: str = Form("skip"), auto_repost: bool = Form(True)):
        tmp = cfg.subdir("imports") / "incoming"
        tmp.mkdir(parents=True, exist_ok=True)
        saved = []
        try:
            for up in (video, clean):
                if up is None or not up.filename:
                    saved.append(None)
                    continue
                dest = tmp / Path(up.filename).name
                with open(dest, "wb") as fh:
                    shutil.copyfileobj(up.file, fh, 8 * 1024 * 1024)
                saved.append(dest)
            try:
                when = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(400, "Pick the date (and time) the reel was first posted")
            try:
                result = import_reel(cfg, conn(), saved[0], when, title=title, caption=caption,
                                     platforms=[p for p in platforms.split(",") if p], done_steps=[d for d in done.split(",") if d],
                                     has_text=has_text, clean=saved[1], missed=missed, auto_repost=auto_repost)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            except RuntimeError as exc:
                log.exception("Import failed")
                raise HTTPException(400, f"Couldn't read that video: {exc}")
            return result
        finally:
            for f in saved:
                if f is not None:
                    f.unlink(missing_ok=True)

    @app.get("/api/events")
    def events(limit: int = 100):
        return [dict(r) for r in conn().execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    return app
