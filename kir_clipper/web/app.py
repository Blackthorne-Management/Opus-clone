"""Local review dashboard: http://127.0.0.1:8765"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from .. import db
from ..config import Config
from ..performance import score_report
from ..pipeline import approve_clip, edit_clip, reject_clip
from ..scheduler import copy_for, next_slot

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
        counts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM clips GROUP BY status")}
        scores = [r["score"] for r in c.execute("SELECT score FROM clips WHERE parent_clip_id IS NULL")]
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
            """SELECT p.*, c.title, c.score FROM posts p JOIN clips c ON c.id=p.clip_id
               ORDER BY CASE WHEN p.status IN ('planned','submitted') THEN 0 ELSE 1 END,
                        CASE WHEN p.status IN ('planned','submitted') THEN p.publish_at END ASC,
                        p.publish_at DESC LIMIT ?""",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

    @app.post("/api/posts/{post_id}/cancel")
    def cancel(post_id: int):
        c = conn()
        row = c.execute("SELECT status FROM posts WHERE id=?", (post_id,)).fetchone()
        if not row or row["status"] != "planned":
            raise HTTPException(400, "Only planned posts can be cancelled here; submitted posts must be removed in Upload-Post")
        db.update_post(c, post_id, status="cancelled")
        db.log_event(c, "post_cancelled", post_id)
        return {"ok": True}

    @app.get("/api/events")
    def events(limit: int = 100):
        return [dict(r) for r in conn().execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    return app
