"""Niche research: find what is already working for other creators (and for you), from the /virality skill.

Posts come from what you paste or upload (numbers read off your phone, an analytics CSV, a yt-dlp
JSON dump), or from a public YouTube channel's Shorts tab when yt-dlp is installed. Nothing logs in,
uses cookies, or scrapes behind a login.

Each post is ranked by its views as a multiple of its OWN creator's median, so a 400K post on an
account that usually does 30K outranks a 2M post on an account that usually does 2M. A creator needs
at least four posts for a median to mean anything (or a `median` column you supply). The formula
name comes from the hook library in virality/hooks.json; it describes the words, not why it worked.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from . import db
from .virality import collect, formula_name

log = logging.getLogger("kir.research")

MIN_POSTS = 4

def _key(creator: str) -> str:
    return collect.creator_key(creator)


def add_posts(conn, text: str, source: str = "paste", creator: str = "", platform: str = "") -> dict:
    """Parse pasted text / file contents and store new posts. Returns {"added", "duplicates", "skipped"}."""
    try:
        pairs = collect.records_from_text(text, source)
    except ValueError as exc:
        raise ValueError(f"Couldn't read that: {exc}") from exc
    defaults = {"channel": creator.strip() or None, "platform": platform.strip() or None}
    existing_urls = {collect.canon_url(r["url"]) for r in conn.execute("SELECT url FROM research_posts WHERE url IS NOT NULL")}
    existing_keys = {(_key(r["creator"]), (r["hook"] or r["title"] or "").lower())
                     for r in conn.execute("SELECT creator, hook, title FROM research_posts")}
    added = dup = skipped = 0
    for record, parent in pairs:
        post = collect.normalize(record, {**parent, **{k: v for k, v in defaults.items() if v}})
        if post is None:
            skipped += 1
            continue
        url = post["url"] or None
        url_key = collect.canon_url(url) if url else None
        key = (_key(post["channel"]), (post["hook"] or post["title"] or "").lower())
        if (url_key and url_key in existing_urls) or key in existing_keys:
            dup += 1
            continue
        conn.execute(
            """INSERT INTO research_posts(creator, platform, views, likes, comments, hook, title, url, duration, posted,
                   median, source, added_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (post["channel"], post["platform"], post["views"], post["likes"], post["comments"], post["hook"],
             post["title"], url, post["duration"], post["posted"], post["median"], source, db.iso(db.utcnow())))
        existing_keys.add(key)
        if url_key:
            existing_urls.add(url_key)
        added += 1
    db.log_event(conn, "research_added", None, {"added": added, "duplicates": dup, "skipped": skipped, "source": source})
    return {"added": added, "duplicates": dup, "skipped": skipped}


def ytdlp_available() -> bool:
    try:
        import yt_dlp  # noqa: F401
        return True
    except ImportError:
        return False


def channel_url(handle: str) -> str:
    h = handle.strip()
    if h.startswith("http"):
        return h
    return f"https://www.youtube.com/{h if h.startswith('@') else '@' + h}/shorts"


def fetch_channel(conn, handle: str, limit: int = 40, runner=None) -> dict:
    """List a public channel's Shorts with view counts through yt-dlp (no login, no cookies)."""
    if runner is None and not ytdlp_available():
        raise RuntimeError("yt-dlp isn't installed. Install it with: .venv\\Scripts\\python -m pip install yt-dlp")
    url = channel_url(handle)
    cmd = [sys.executable, "-m", "yt_dlp", "--flat-playlist", "--playlist-end", str(int(limit)), "-J", url]
    run = runner or (lambda c: subprocess.run(c, capture_output=True, text=True, encoding="utf-8", timeout=180))
    proc = run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp couldn't read {url}: {(proc.stderr or '').strip()[-300:]}")
    return add_posts(conn, proc.stdout, source="yt-dlp", platform="shorts")


def own_posts(conn, min_age_days: int = 3) -> list[dict]:
    """Your own posted clips with their latest view count, one creator per platform ("You · tiktok")."""
    cutoff = db.iso(db.utcnow() - timedelta(days=min_age_days))
    rows = conn.execute(
        """SELECT p.id, p.platform, p.posted_at, c.hook, c.title, c.recipe_json,
                  (SELECT views FROM performance f WHERE f.post_id=p.id AND f.views IS NOT NULL
                   ORDER BY fetched_at DESC LIMIT 1) AS views
           FROM posts p JOIN clips c ON c.id=p.clip_id
           WHERE p.status='posted' AND c.kind='short' AND p.posted_at <= ? AND COALESCE(p.trial, 0)=0""",
        (cutoff,)).fetchall()
    out = []
    for r in rows:
        if r["views"] is None:
            continue
        recipe = json.loads(r["recipe_json"] or "{}")
        out.append({"creator": f"You · {r['platform']}", "platform": r["platform"], "views": r["views"],
                    "hook": recipe.get("hook_text") or r["hook"], "title": r["title"], "url": None,
                    "posted": (r["posted_at"] or "")[:10], "median": None, "own": True})
    return out


def outliers(conn, min_multiple: float = 2.0, days: int | None = 90, include_own: bool = True,
             now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    rows = [dict(r) for r in conn.execute("SELECT * FROM research_posts WHERE views IS NOT NULL")]
    if days:
        since = (now - timedelta(days=days)).date().isoformat()
        rows = [r for r in rows if not r["posted"] or r["posted"] >= since]
    if include_own:
        rows += own_posts(conn)
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(_key(r["creator"]), []).append(r)
    ranked, thin, creators = [], [], []
    for key, posts in by.items():
        supplied = next((p["median"] for p in posts if p.get("median")), None)
        name = posts[0]["creator"]
        if len(posts) < MIN_POSTS and not supplied:
            thin.append({"creator": name, "posts": len(posts)})
            continue
        med = float(supplied or statistics.median(p["views"] for p in posts))
        creators.append({"creator": name, "posts": len(posts), "median": int(med)})
        for p in posts:
            m = p["views"] / med if med else 0.0
            ranked.append({"id": p.get("id"), "creator": name, "platform": p.get("platform"), "views": p["views"],
                           "median": int(med), "multiple": round(m, 2), "hook": p.get("hook") or p.get("title") or "",
                           "title": p.get("title") or "", "url": p.get("url"), "posted": p.get("posted"),
                           "formula": formula_name(p.get("hook") or p.get("title") or ""), "own": bool(p.get("own"))})
    winners = sorted((r for r in ranked if r["multiple"] >= min_multiple), key=lambda r: -r["multiple"])
    counts: dict[str, int] = {}
    for r in winners:
        counts[r["formula"]] = counts.get(r["formula"], 0) + 1
    total = sum(c["posts"] for c in creators)
    evidence = ("not enough yet" if total < 12 else "an anecdote" if total < 30 or len(creators) < 4 else "real evidence")
    return {"outliers": winners, "formulas": sorted(counts.items(), key=lambda kv: -kv[1]), "thin": thin,
            "creators": sorted(creators, key=lambda c: -c["posts"]), "posts": total,
            "evidence": evidence, "min_multiple": min_multiple, "days": days}


def remove(conn, creator: str | None = None, post_id: int | None = None) -> int:
    if post_id is not None:
        return conn.execute("DELETE FROM research_posts WHERE id=?", (post_id,)).rowcount
    if creator:
        ids = [r["id"] for r in conn.execute("SELECT id, creator FROM research_posts") if _key(r["creator"]) == _key(creator)]
        for i in ids:
            conn.execute("DELETE FROM research_posts WHERE id=?", (i,))
        return len(ids)
    return conn.execute("DELETE FROM research_posts").rowcount


def get_post(conn, post_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM research_posts WHERE id=?", (post_id,)).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- winner breakdown

BREAKDOWN_SCHEMA = {
    "type": "object",
    "properties": {
        "winners": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "format": {"type": "string"}, "structure": {"type": "string"},
                           "cta": {"type": "string"}, "pace": {"type": "string"}},
            "required": ["id", "format", "structure", "cta", "pace"], "additionalProperties": False}},
        "shared": {"type": "string"},
        "how_to_use_it": {"type": "string"},
    },
    "required": ["winners", "shared", "how_to_use_it"],
    "additionalProperties": False,
}


def youtube_transcript(url: str, runner=None) -> str | None:
    """Auto-captions of a public YouTube video as plain text (no login), or None."""
    if not url or not re.search(r"(youtube\.com|youtu\.be)/", url) or (runner is None and not ytdlp_available()):
        return None
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory(prefix="kir_subs_") as tmp:
        cmd = [sys.executable, "-m", "yt_dlp", "--skip-download", "--write-auto-subs", "--write-subs",
               "--sub-langs", "en.*,en", "--sub-format", "json3", "-o", str(Path(tmp) / "v.%(ext)s"), url]
        run = runner or (lambda c: subprocess.run(c, capture_output=True, text=True, encoding="utf-8", timeout=120))
        try:
            proc = run(cmd)
        except Exception:
            return None
        if getattr(proc, "returncode", 1) != 0:
            return None
        for f in sorted(Path(tmp).glob("*.json3")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            text = " ".join(s.get("utf8", "") for e in data.get("events", []) for s in (e.get("segs") or []))
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                return text[:4000]
    return None


def breakdown(cfg, conn, llm, top: int = 5, min_multiple: float = 2.0, runner=None) -> dict:
    """Break the top outliers down (format, structure, CTA, pace) and name the one thing they share."""
    rep = outliers(conn, min_multiple=min_multiple, days=None, include_own=True)
    picks = [o for o in rep["outliers"] if o.get("id")][:top]
    if not picks:
        raise ValueError("No outliers to break down yet. Add more posts first.")
    rest = [o for o in outliers(conn, min_multiple=0, days=None)["outliers"]
            if o.get("id") and o["id"] not in {p["id"] for p in picks}][:15]
    blocks = []
    for o in picks:
        row = get_post(conn, o["id"]) or {}
        transcript = youtube_transcript(o.get("url") or "", runner)
        blocks.append(f'<winner id="{o["id"]}" creator="{o["creator"]}" views="{o["views"]}" '
                      f'multiple="{o["multiple"]}" formula="{o["formula"]}" seconds="{row.get("duration") or "unknown"}">\n'
                      f'hook/title: {o["hook"]}\n' + (f"transcript: {transcript}\n" if transcript else "") + "</winner>")
    others = "\n".join(f'- {o["creator"]} ({o["multiple"]}x): {o["hook"]}' for o in rest) or "(none)"
    result = llm(
        f"You study short-form videos that beat their creator's usual views, for {cfg.get('brand.name', 'a creator')}.",
        f"""These posts did far better than each creator's own median:

{chr(10).join(blocks)}

For comparison, ordinary posts from the same batch:
{others}

For each winner (by id) say: format (talking head, split screen, green screen, screen-share tutorial, listicle,
skit, b-roll voice-over...; say "unknown" if the hook alone can't tell you), structure (which beats in what order:
hook, proof, steps, rehook, payoff, CTA), the CTA (the exact ask and where it appears, or "unknown"), and pace
(length and speed if known). Then `shared`: the ONE thing the winners share that the ordinary posts don't.
`how_to_use_it`: one or two sentences on how this creator should use it. Only claim what the text supports;
the formula is a judgement about the words, not proof of why a post got its views.""",
        BREAKDOWN_SCHEMA)
    by_id = {p["id"]: p for p in picks}
    result["winners"] = [{**w, **{k: by_id[w["id"]][k] for k in ("creator", "hook", "multiple", "formula")}}
                         for w in result["winners"] if w["id"] in by_id]
    db.log_event(conn, "research_breakdown", None, {"winners": [p["id"] for p in picks]})
    (cfg.data_dir / "research_breakdown.json").write_text(json.dumps(result), encoding="utf-8")
    return result


def last_breakdown(cfg) -> dict | None:
    p = cfg.data_dir / "research_breakdown.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
