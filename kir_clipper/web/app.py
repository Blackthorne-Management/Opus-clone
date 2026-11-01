"""Local review dashboard: http://127.0.0.1:8765"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .. import db
from .. import settings as settings_mod
from ..cadence import step_label, timeline
from ..config import Config
from ..importer import cadence_steps, import_reel
from ..longform import apply_edits, set_segments
from ..maintenance import free_gb, service_status
from ..performance import score_report
from ..pipeline import approve_clip, edit_clip, reject_clip
from ..scheduler import copy_for, mark_posted, next_slot

log = logging.getLogger("kir.web")
HERE = Path(__file__).parent


LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}

LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>KIR Clipper</title>
<style>body{margin:0;font:16px system-ui,sans-serif;background:#f6f6f4;color:#1b1b1b;display:grid;place-items:center;min-height:100vh}
@media (prefers-color-scheme: dark){body{background:#121212;color:#eee}}
form{display:flex;flex-direction:column;gap:12px;width:min(320px,90vw)}input,button{font:inherit;padding:12px;border-radius:8px;border:1px solid #bbb}
button{background:#2a9d5c;color:#fff;border:0;font-weight:600}.e{color:#e63946;min-height:1.2em}</style></head>
<body><form method="post" action="/login"><h2 style="margin:0">KIR Clipper</h2><label>Enter your PIN</label>
<input name="pin" type="password" inputmode="numeric" autocomplete="current-password" autofocus>
<div class="e">{{error}}</div><button>Open dashboard</button></form></body></html>"""


def _session_token(cfg: Config):
    """Cookie value = HMAC of the PIN with a random key stored in the data folder. Changing the PIN signs everyone out."""
    import hashlib
    import hmac
    import secrets as _secrets

    key_file = cfg.data_dir / ".session_key"
    if not key_file.exists():
        key_file.write_text(_secrets.token_hex(32), encoding="utf-8")
    key = key_file.read_text(encoding="utf-8").strip().encode()
    return lambda: hmac.new(key, str(cfg.get("dashboard.pin") or "").encode(), hashlib.sha256).hexdigest()


def _secs(value: str) -> float | None:
    """'75', '1:15' or '0:01:15' -> seconds; blank -> None."""
    value = (value or "").strip()
    if not value:
        return None
    total = 0.0
    for part in value.split(":"):
        total = total * 60 + float(part)
    return total


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="KIR Clipper", docs_url=None, redoc_url=None)
    session_token = _session_token(cfg)
    failures: dict[str, list[float]] = {}

    @app.middleware("http")
    async def phone_access_guard(request: Request, call_next):
        """Requests from this PC are always allowed. Other devices (your phone) need the PIN cookie."""
        host = request.client.host if request.client else ""
        if (not cfg.get("dashboard.lan_access") or host in LOCAL_HOSTS or request.url.path == "/login"
                or request.cookies.get("kir_session") == session_token()):
            return await call_next(request)
        if request.url.path.startswith(("/api/", "/media/")):
            return HTMLResponse("Sign in with your PIN first", status_code=401)
        return RedirectResponse("/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(error: str = ""):
        return LOGIN_HTML.replace("{{error}}", error)

    @app.post("/login")
    def login(request: Request, pin: str = Form("")):
        import time as _time
        host = request.client.host if request.client else "?"
        recent = [t for t in failures.get(host, []) if _time.time() - t < 300]
        if len(recent) >= 5:
            return RedirectResponse("/login?error=Too+many+tries.+Wait+5+minutes.", status_code=303)
        if pin and pin == str(cfg.get("dashboard.pin") or ""):
            failures.pop(host, None)
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie("kir_session", session_token(), max_age=90 * 86400, httponly=True, samesite="lax")
            return resp
        failures[host] = recent + [_time.time()]
        return RedirectResponse("/login?error=Wrong+PIN", status_code=303)

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
            "brand": cfg.get("brand.name") or "",
            "setup_done": bool(cfg.get("setup.done")),
            "dry_run": cfg.dry_run,
            "auto_post_threshold": cfg.get("review.auto_post_threshold"),
            "auto_reject_floor": cfg.get("review.auto_reject_floor"),
            "platforms": cfg.get("publishing.platforms"),
            "timezone": cfg.get("publishing.upload_post.timezone") or "UTC",
            "clips": counts, "posts": posts, "streams": streams,
            "histogram": [sum(1 for s in scores if b * 10 <= s < b * 10 + 10) for b in range(10)],
            "performance": score_report(c),
            "manual_due": c.execute("SELECT COUNT(*) FROM posts WHERE status='manual'").fetchone()[0],
            "service": service_status(c),
            "free_gb": round(free_gb(cfg.data_dir), 1),
            "min_free_gb": float(cfg.get("maintenance.min_free_gb", 20)),
            "optimize_min": int(cfg.get("optimize.min_posts", 20)),
            "processing": [dict(r) for r in c.execute(
                "SELECT id, original_path FROM streams WHERE status='processing' ORDER BY id")],
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
            edited = (d.get("checks") or {}).get("edit") or {}
            d["duration"] = round(edited.get("duration_out") or (d["end"] - d["start"]), 1)
            d["timeline"] = timeline(cfg, c, r["id"]) if d["posts"] else []
            out.append(d)
        return out

    @app.get("/media/clip/{clip_id}")
    def clip_media(clip_id: int):
        row = db.get_clip(conn(), clip_id)
        if not row or not row["file_path"] or not Path(row["file_path"]).exists():
            raise HTTPException(404)
        return FileResponse(row["file_path"], media_type="video/mp4")

    @app.get("/media/clip/{clip_id}/srt")
    def clip_srt(clip_id: int):
        row = db.get_clip(conn(), clip_id)
        srt = Path(row["file_path"]).with_suffix(".srt") if row and row["file_path"] else None
        if not srt or not srt.exists():
            raise HTTPException(404, "No caption file for this clip (re-render it to make one)")
        return FileResponse(srt, media_type="application/x-subrip", filename=f"clip_{clip_id}.srt")

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
        c2 = conn()
        for r in rows:
            d = dict(r)
            d["label"] = step_label(d["step"], cfg) if d["kind"] == "short" else "Long-form video"
            perf = c2.execute("SELECT views, likes FROM performance WHERE post_id=? ORDER BY fetched_at DESC LIMIT 1",
                              (d["id"],)).fetchone()
            d["views"], d["likes"] = (perf["views"], perf["likes"]) if perf else (None, None)
            out.append(d)
        return out

    @app.post("/api/posts/{post_id}/mark-posted")
    def post_mark_posted(post_id: int, body: dict = Body(default={})):
        try:
            mark_posted(conn(), post_id, body.get("url"))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}

    @app.post("/api/posts/{post_id}/metrics")
    def post_metrics(post_id: int, body: dict = Body(...)):
        from ..performance import METRICS, record
        vals = {}
        for k in METRICS:
            v = body.get(k)
            if v not in (None, ""):
                try:
                    vals[k] = float(str(v).replace(",", "")) if k == "watch_time" else int(float(str(v).replace(",", "")))
                except ValueError:
                    raise HTTPException(400, f"{k} must be a number")
        if not vals:
            raise HTTPException(400, "Enter at least the views")
        c = conn()
        if not c.execute("SELECT 1 FROM posts WHERE id=? AND status='posted'", (post_id,)).fetchone():
            raise HTTPException(400, "Only posted posts can have numbers")
        record(c, post_id, source="manual", **vals)
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
                d["thumbnails"] = [{k: t.get(k) for k in ("text", "why", "layout", "source", "ai_error")}
                                   for t in plan.get("thumbnails", [])]
                d["thumbnail_choice"] = plan.get("thumbnail_choice", 0)
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

    @app.get("/media/thumb/{lf_id}/{n}")
    def thumb_media(lf_id: int, n: int, download: int = 0):
        row = conn().execute("SELECT plan_json FROM longform WHERE id=?", (lf_id,)).fetchone()
        thumbs = (json.loads(row["plan_json"] or "{}") if row else {}).get("thumbnails", [])
        if not 0 <= n < len(thumbs) or not Path(thumbs[n]["path"]).exists():
            raise HTTPException(404)
        path = Path(thumbs[n]["path"])
        return FileResponse(path, media_type="image/jpeg", filename=path.name if download else None)

    @app.post("/api/longform/{lf_id}/thumbnail")
    def longform_thumbnail(lf_id: int, body: dict = Body(...)):
        from ..longform import make_thumbnails
        c = conn()
        row = c.execute("SELECT * FROM longform WHERE id=?", (lf_id,)).fetchone()
        if not row or not row["plan_json"]:
            raise HTTPException(404)
        plan = json.loads(row["plan_json"])
        if body.get("regenerate"):
            stream = c.execute("SELECT id FROM streams WHERE id=?", (row["stream_id"],)).fetchone()
            out_dir = cfg.subdir("longform") / f"stream_{stream['id']:04d}"
            try:
                make_thumbnails(cfg, plan, out_dir, f"part{row['idx']}_lf{lf_id}")
            except Exception as exc:
                log.exception("Thumbnail regeneration failed")
                raise HTTPException(500, f"Couldn't make new thumbnails: {exc}")
        elif body.get("choice") is not None:
            n = int(body["choice"])
            if not 0 <= n < len(plan.get("thumbnails", [])):
                raise HTTPException(400, "No such thumbnail")
            plan["thumbnail_choice"] = n
        db.update_longform(c, lf_id, plan_json=plan)
        db.log_event(c, "thumbnail_changed", lf_id, {k: body[k] for k in body})
        return {"ok": True, "thumbnails": plan.get("thumbnails", []), "choice": plan.get("thumbnail_choice", 0)}

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
                      missed: str = Form("skip"), auto_repost: bool = Form(True),
                      clean_start: str = Form(""), clean_end: str = Form("")):
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
                                     has_text=has_text, clean=saved[1], missed=missed, auto_repost=auto_repost,
                                     clean_start=_secs(clean_start), clean_end=_secs(clean_end))
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            except ImportError:
                log.exception("Import failed")
                raise HTTPException(500, "Transcription isn't installed on this PC, so the clean copy can't be "
                                         "captioned. Run scripts\\install.ps1 again (or import without the clean copy).")
            except RuntimeError as exc:
                log.exception("Import failed")
                raise HTTPException(400, f"Couldn't read that video: {exc}")
            except Exception as exc:
                log.exception("Import failed")
                raise HTTPException(500, f"Import failed: {exc}. Nothing was saved; you can try again.")
            return result
        finally:
            for f in saved:
                if f is not None:
                    f.unlink(missing_ok=True)

    @app.get("/api/optimize")
    def optimize_get():
        from ..optimize import suggestions
        return suggestions(conn(), cfg)

    @app.post("/api/optimize/apply")
    def optimize_apply(body: dict = Body(...)):
        from ..optimize import suggest
        platform = body.get("platform")
        if platform not in (cfg.get("publishing.platforms") or []):
            raise HTTPException(400, "Unknown platform")
        sug = suggest(conn(), cfg, platform)
        if not sug["enough"]:
            raise HTTPException(400, "Not enough results yet to change the times")
        settings_mod.update(cfg, {f"publishing.slots_by_platform.{platform}": sug["suggested"]})
        db.log_event(conn(), "posting_times_applied", platform, sug["suggested"])
        return {"ok": True, "slots": sug["suggested"]}

    @app.get("/api/hooktests")
    def hooktests_get():
        from ..hooktests import evaluate, summary as ab_summary
        c = conn()
        evaluate(c, cfg)
        return ab_summary(c)

    @app.get("/api/setup")
    def setup_get():
        from ..onboarding import current
        return current(cfg)

    @app.post("/api/setup")
    async def setup_post(name: str = Form(""), cta_text: str = Form(""), timezone: str = Form(""),
                         upload_post_user: str = Form(""), inbox: str = Form(""), criteria: str = Form(""),
                         topics: str = Form(""), wants: str = Form(""), logo: UploadFile | None = File(None)):
        from ..onboarding import is_image, save
        logo_bytes = None
        if logo is not None and logo.filename:
            logo_bytes = await logo.read()
            if not is_image(logo_bytes):
                raise HTTPException(400, "The logo must be a PNG file (a transparent background looks best)")
        try:
            result = save(cfg, {"name": name, "cta_text": cta_text, "timezone": timezone,
                                "upload_post_user": upload_post_user, "inbox": inbox, "criteria": criteria,
                                "topics": topics, "wants": wants}, logo_bytes)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        db.log_event(conn(), "setup_saved", None, {"name": name})
        return {"ok": True, **result}

    @app.get("/api/settings")
    def get_settings():
        from ..maintenance import dashboard_url
        url = dashboard_url(cfg)
        return {"fields": settings_mod.current(cfg), "phone_url": None if "127.0.0.1" in url else url}

    @app.post("/api/settings")
    def set_settings(body: dict = Body(...)):
        try:
            changed = settings_mod.update(cfg, body)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc))
        c = conn()
        db.log_event(c, "settings_changed", None, changed)
        return {"ok": True, "changed": changed}

    # ------------------------------------------------------------ niche research

    @app.get("/api/research")
    def research_get(min: float = 2.0, days: int = 90, own: int = 1):
        from ..research import outliers, ytdlp_available
        return {**outliers(conn(), min_multiple=min, days=days or None, include_own=bool(own)),
                "ytdlp": ytdlp_available()}

    @app.post("/api/research/add")
    async def research_add(text: str = Form(""), creator: str = Form(""), platform: str = Form(""),
                           file: UploadFile | None = File(None)):
        from ..research import add_posts
        if file is not None and file.filename:
            text = (await file.read()).decode("utf-8-sig", errors="replace")
        if not text.strip():
            raise HTTPException(400, "Paste some posts or choose a file")
        try:
            return add_posts(conn(), text, source="file" if file is not None and file.filename else "paste",
                             creator=creator, platform=platform)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/research/fetch")
    def research_fetch(body: dict = Body(...)):
        from ..research import fetch_channel
        handle = str(body.get("channel") or "").strip()
        if not handle:
            raise HTTPException(400, "Enter a YouTube @handle or channel link")
        try:
            return fetch_channel(conn(), handle, int(body.get("limit") or 40))
        except RuntimeError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/research/breakdown")
    def research_breakdown(body: dict = Body(default={})):
        from ..research import breakdown
        try:
            return breakdown(cfg, conn(), _claude(), min_multiple=float(body.get("min") or 2.0))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/research/breakdown")
    def research_breakdown_last():
        from ..research import last_breakdown
        return last_breakdown(cfg) or {}

    @app.post("/api/research/remove")
    def research_remove(body: dict = Body(...)):
        from ..research import remove
        n = remove(conn(), creator=body.get("creator"), post_id=body.get("id"))
        return {"ok": True, "removed": n}

    # ------------------------------------------------------------ script studio

    @app.get("/api/studio")
    def studio_get():
        from ..studio import list_scripts, profile_wpm, read_profile
        from ..config import secret
        c = conn()
        prof = read_profile(cfg)
        return {"profile": prof, "wpm": profile_wpm(cfg, c, prof["voice"]), "scripts": list_scripts(c),
                "claude": bool(secret("ANTHROPIC_API_KEY"))}

    @app.post("/api/studio/profile")
    def studio_profile(body: dict = Body(...)):
        from ..studio import save_profile
        save_profile(cfg, voice=body.get("voice"), audience=body.get("audience"))
        db.log_event(conn(), "profile_saved", None, [k for k in ("voice", "audience") if body.get(k) is not None])
        return {"ok": True}

    def _claude():
        from ..llm import ClaudeJSON, LLMError
        try:
            return ClaudeJSON(cfg)
        except LLMError as exc:
            raise HTTPException(400, f"{exc}. The Script studio needs Claude.")

    @app.post("/api/studio/voice")
    def studio_voice(body: dict = Body(default={})):
        from ..studio import build_voice
        try:
            return {"voice": build_voice(cfg, conn(), _claude(), body.get("samples") or "")}
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/studio/scripts")
    def studio_new(body: dict = Body(...)):
        from ..research import get_post, outliers
        from ..studio import start_script
        based_on = None
        if body.get("research_id"):
            c = conn()
            post = get_post(c, int(body["research_id"]))
            if not post:
                raise HTTPException(404, "That research post is gone")
            ranked = {r["id"]: r for r in outliers(c, min_multiple=0, days=None, include_own=False)["outliers"]}
            based_on = ranked.get(post["id"]) or {"creator": post["creator"], "views": post["views"],
                                                  "hook": post["hook"] or post["title"], "multiple": None,
                                                  "formula": None}
            from ..research import last_breakdown
            bd = last_breakdown(cfg) or {}
            mine = next((w for w in bd.get("winners", []) if w.get("id") == post["id"]), None)
            if mine:
                based_on = {**based_on, "breakdown": {k: mine[k] for k in ("format", "structure", "cta", "pace")},
                            "shared": bd.get("shared")}
        llm = _claude()
        try:
            sid = start_script(cfg, conn(), body.get("idea") or "", based_on, llm=llm)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "id": sid}

    @app.post("/api/studio/scripts/{script_id}/status")
    def studio_status(script_id: int, body: dict = Body(...)):
        if body.get("status") not in ("ready", "shot"):
            raise HTTPException(400, "status must be ready or shot")
        db.update_script(conn(), script_id, status=body["status"])
        return {"ok": True}

    @app.get("/api/studio/scripts/{script_id}.md")
    def studio_markdown(script_id: int):
        from fastapi.responses import PlainTextResponse
        row = conn().execute("SELECT script_json FROM scripts WHERE id=?", (script_id,)).fetchone()
        if not row or not row["script_json"]:
            raise HTTPException(404)
        return PlainTextResponse(json.loads(row["script_json"])["markdown"], media_type="text/markdown",
                                 headers={"Content-Disposition": f'attachment; filename="script_{script_id}.md"'})

    # ------------------------------------------------------------ edit a video

    @app.get("/api/edits")
    def edits_get():
        from ..config import secret
        from ..editor import list_edits
        try:
            import faster_whisper  # noqa: F401
            local_stt = True
        except ImportError:
            local_stt = False
        return {"edits": list_edits(conn()), "claude": bool(secret("ANTHROPIC_API_KEY")),
                "transcription": local_stt or cfg.get("transcription.backend") == "openai"}

    @app.post("/api/edits")
    def edits_new(files: list[UploadFile] = File(...), sound: UploadFile | None = File(None),
                  options: str = Form("{}")):
        from ..editor import create, kick, safe_name
        tmp = cfg.subdir("edits") / "incoming"
        tmp.mkdir(parents=True, exist_ok=True)
        saved, sound_path = [], None
        try:
            for up in files:
                if not up.filename:
                    continue
                dest = tmp / safe_name(up.filename)
                with open(dest, "wb") as fh:
                    shutil.copyfileobj(up.file, fh, 8 * 1024 * 1024)
                saved.append(dest)
            if sound is not None and sound.filename:
                sound_path = tmp / ("sound_" + safe_name(sound.filename))
                with open(sound_path, "wb") as fh:
                    shutil.copyfileobj(sound.file, fh, 8 * 1024 * 1024)
            try:
                edit_id = create(cfg, conn(), saved, json.loads(options or "{}"), sound_path)
            except (ValueError, json.JSONDecodeError) as exc:
                raise HTTPException(400, str(exc))
        finally:
            for f in saved + ([sound_path] if sound_path else []):
                f.unlink(missing_ok=True)   # moved into the edit's folder on success
        kick(cfg)
        return {"ok": True, "id": edit_id}

    @app.post("/api/edits/{edit_id}/retry")
    def edits_retry(edit_id: int):
        from ..editor import kick, retry
        try:
            retry(conn(), edit_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        kick(cfg)
        return {"ok": True}

    @app.get("/api/events")
    def events(limit: int = 100):
        return [dict(r) for r in conn().execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    return app
