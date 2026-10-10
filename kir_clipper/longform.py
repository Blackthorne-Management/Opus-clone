"""Long-form YouTube edit of the full stream.

The stream stays mostly intact. Only moments where the creator isn't actually delivering content are
removed (distracted, interrupted, reading chat aloud, tech issues, away, housekeeping), plus long dead-air
pauses are shortened. On top of that: an intro, chapter title cards, lower-third topic banners, pop-up
key points, punch-in zooms, a subscribe reminder, a watermark and an outro. Chapters, title, description
and tags are written for YouTube.

A recording can be split into several segments (each becomes its own video); the owner confirms or edits
the segments in the dashboard before anything is planned or rendered.

Render strategy (frame-exact, so audio never drifts out of sync over a 3-hour video):
  1. every kept range is rendered as a video-only .ts part with an exact frame count, overlays already
     burned in using the final video's timeline;
  2. the matching audio is extracted per range as PCM with an exact sample count (fps divides 48 kHz);
  3. parts are concatenated without re-encoding, the audio is loudness-normalised in one pass, and muxed.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db, media
from .captions import _esc, _ts, ass_color
from .config import Config
from .highlights import build_sentences, fmt_ts, render_sentences, windows
from .notify import notify
from .render import filter_path

log = logging.getLogger("kir.longform")

SAMPLE_RATE = 48000

CATEGORY_HELP = {
    "distracted": "the creator is clearly not engaged with the audience: looking at something else, a side "
                  "conversation, muttering to themselves",
    "interrupted": "cut off by something (door, phone, someone walking in), or abandoning a sentence to restart it",
    "chat_reading": "reading chat usernames, comments or shout-outs aloud, thanking for follows/subs/donations. "
                    "If a chat question leads into a real answer, cut only the reading-out, never the answer",
    "tech_issue": "audio/video problems, 'can you hear me', fixing OBS or the mic, waiting on something to load",
    "away": "AFK, a break, 'be right back', long silence while gone",
    "housekeeping": "'starting soon', setting up the stream, ad reads, end-of-stream logistics",
}


def lf(cfg: Config, key: str, default=None):
    return cfg.get(f"longform.{key}", default)


# =========================================================================== segments

def suggest_segments(words: list[dict], duration: float, split_gap_s: float) -> list[dict]:
    """One segment from first to last word, split wherever nobody speaks for `split_gap_s`."""
    if not words:
        return [{"idx": 1, "title": "Part 1", "start": 0.0, "end": round(duration, 2)}]
    bounds = [max(0.0, words[0]["s"] - 1.0)]
    for a, b in zip(words, words[1:]):
        if b["s"] - a["e"] >= split_gap_s:
            bounds += [a["e"] + 1.0, b["s"] - 1.0]
    bounds.append(min(duration, words[-1]["e"] + 1.5))
    segs = [{"start": round(bounds[i], 2), "end": round(bounds[i + 1], 2)} for i in range(0, len(bounds), 2)]
    segs = [s for s in segs if s["end"] - s["start"] >= 60] or segs[:1]
    return [{"idx": i + 1, "title": f"Part {i + 1}", **s} for i, s in enumerate(segs)]


def prepare_segments(cfg: Config, conn, stream_id: int, transcript: dict, duration: float) -> list[int]:
    """Called after a stream is processed: create the long-form project for the owner to confirm."""
    if not lf(cfg, "enabled", True):
        return []
    conn.execute("DELETE FROM longform WHERE stream_id=? AND status IN ('needs_segments','queued','failed')",
                 (stream_id,))
    segs = suggest_segments(transcript["words"], duration, float(lf(cfg, "suggest_split_gap_minutes", 3)) * 60)
    status = "needs_segments" if lf(cfg, "wait_for_segments", True) else "queued"
    ids = [_insert_segment(conn, stream_id, s, status) for s in segs]
    if status == "needs_segments":
        extra = f" ({len(segs)} suggested segments)" if len(segs) > 1 else ""
        notify(cfg, "Long-form edit: confirm segments",
               f"Open the dashboard's Long-form tab to confirm or split stream {stream_id}{extra}.")
    db.log_event(conn, "longform_prepared", stream_id, {"segments": segs, "status": status})
    return ids


def _insert_segment(conn, stream_id: int, seg: dict, status: str) -> int:
    now = db.iso(db.utcnow())
    cur = conn.execute(
        """INSERT INTO longform(stream_id, idx, title, start, "end", status, replan, created_at, updated_at)
           VALUES (?,?,?,?,?,?,1,?,?)""",
        (stream_id, seg["idx"], seg.get("title"), float(seg["start"]), float(seg["end"]), status, now, now))
    return cur.lastrowid


def set_segments(conn, stream_id: int, segments: list[dict], duration: float | None = None) -> list[int]:
    """Replace a stream's segments with the owner's choice and queue them for editing."""
    locked = conn.execute(
        """SELECT COUNT(*) FROM longform l JOIN clips c ON c.id = l.clip_id
           WHERE l.stream_id=? AND c.status IN ('approved','scheduled','posted')""", (stream_id,)).fetchone()[0]
    if locked:
        raise ValueError("A long-form video from this stream is already scheduled or posted")
    clean = []
    for s in sorted(segments, key=lambda s: float(s["start"])):
        start, end = float(s["start"]), float(s["end"])
        if duration:
            start, end = max(0.0, start), min(duration, end)
        if end - start < 30:
            raise ValueError("Each segment must be at least 30 seconds long")
        if clean and start < clean[-1]["end"]:
            raise ValueError("Segments overlap")
        clean.append({"start": start, "end": end, "title": (s.get("title") or "").strip()})
    if not clean:
        raise ValueError("At least one segment is required")
    old = conn.execute("SELECT clip_id FROM longform WHERE stream_id=? AND clip_id IS NOT NULL", (stream_id,)).fetchall()
    conn.execute("DELETE FROM longform WHERE stream_id=?", (stream_id,))
    for r in old:
        db.update_clip(conn, r["clip_id"], status="rejected", note="superseded by new segments")
    ids = [_insert_segment(conn, stream_id, {**s, "idx": i + 1, "title": s["title"] or f"Part {i + 1}"}, "queued")
           for i, s in enumerate(clean)]
    db.log_event(conn, "longform_segments_set", stream_id, clean)
    return ids


# =========================================================================== planning

def _plan_schema(categories: list[str]) -> dict:
    def arr(props: dict) -> dict:
        return {"type": "array", "items": {"type": "object", "properties": props, "required": list(props),
                                           "additionalProperties": False}}
    i, s = {"type": "integer"}, {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "cuts": arr({"start_idx": i, "end_idx": i, "category": {"type": "string", "enum": categories},
                         "confidence": i, "reason": s}),
            "chapters": arr({"idx": i, "title": s}),
            "lower_thirds": arr({"idx": i, "title": s, "subtitle": s}),
            "popups": arr({"idx": i, "text": s}),
            "zooms": arr({"idx": i}),
        },
        "required": ["cuts", "chapters", "lower_thirds", "popups", "zooms"],
        "additionalProperties": False,
    }


META_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "description": {"type": "string"},
                   "tags": {"type": "array", "items": {"type": "string"}}},
    "required": ["title", "description", "tags"],
    "additionalProperties": False,
}


def _system(cfg: Config, categories: list[str]) -> str:
    cats = "\n".join(f"- {c}: {CATEGORY_HELP.get(c, c)}" for c in categories)
    return f"""You are the long-form video editor for {cfg.get('brand.name', 'the creator')}'s livestream replays on
YouTube. Viewers want the real stream, so the edit stays MOSTLY INTACT and natural. Remove a moment only
when the creator is not actually delivering content, in one of these categories:
{cats}
Never cut for pacing, opinion, or because a section is less exciting. When in doubt, keep it.

Transcript lines look like `[index h:mm:ss +NdB] text`; `+NdB` marks unusually loud sentences.
Always refer to sentences by index."""


def _window_prompt(chunk: list[dict]) -> str:
    return f"""<transcript>
{render_sentences(chunk)}
</transcript>

For this part of the stream, return:
1. cuts: sentence ranges (inclusive) to remove, each with a category, confidence 0-100 that it is truly
   non-content, and a short reason. Cut whole sentences only. Usually there are few or none.
2. chapters: the sentence where a genuinely new topic starts, with a 2-6 word chapter title. Expect a
   new chapter every 5-15 minutes, not more often.
3. lower_thirds: at the start of a major topic, a banner with a 2-5 word title and a 3-8 word subtitle.
4. popups: on-screen key points for the strongest takeaways or quotable lines, at most 6 words each,
   no more than one every 2 minutes.
5. zooms: sentences with real emphasis or a punchline (a quick punch-in zoom), no more than one per minute.
Only use indices shown above."""


def _merge_ranges(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _spaced(items: list[dict], gap: float) -> list[dict]:
    out = []
    for it in sorted(items, key=lambda x: x["t"]):
        if not out or it["t"] - out[-1]["t"] >= gap:
            out.append(it)
    return out


def plan_segment(cfg: Config, llm, words: list[dict], energy: list[float] | None, seg: dict) -> dict:
    sentences = build_sentences(words, energy)
    inside = [s for s in sentences if s["start"] >= seg["start"] - 0.01 and s["end"] <= seg["end"] + 0.01]
    if not inside:
        raise ValueError("No speech found in this segment")
    by_idx = {s["i"]: s for s in inside}
    categories = list(lf(cfg, "cut_categories") or CATEGORY_HELP)
    schema = _plan_schema(categories)
    system = _system(cfg, categories)
    wins = windows(inside, float(lf(cfg, "window_minutes", 20)) * 60, 60)
    log.info("Planning long-form segment %s-%s: %d sentences in %d windows",
             fmt_ts(seg["start"]), fmt_ts(seg["end"]), len(inside), len(wins))
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda w: llm(system, _window_prompt(w), schema), wins))

    def t(idx):
        s = by_idx.get(int(idx))
        return s["start"] if s else None

    min_conf = int(lf(cfg, "min_cut_confidence", 70))
    raw_cuts = []
    for r in results:
        for c in r.get("cuts", []):
            a, b = by_idx.get(int(c["start_idx"])), by_idx.get(int(c["end_idx"]))
            if not a or not b or b["i"] < a["i"] or c["category"] not in categories:
                continue
            raw_cuts.append({"start": a["start"], "end": b["end"], "category": c["category"],
                             "confidence": max(0, min(100, int(c["confidence"]))), "reason": c["reason"]})
    # Overlapping windows can report the same cut twice: keep the highest-confidence version of overlaps.
    cuts: list[dict] = []
    for c in sorted(raw_cuts, key=lambda c: -c["confidence"]):
        if all(c["end"] <= k["start"] or c["start"] >= k["end"] for k in cuts):
            cuts.append(c)
    budget = (seg["end"] - seg["start"]) * float(lf(cfg, "max_cut_ratio", 0.25))
    used = 0.0
    for c in cuts:  # already sorted by confidence
        length = c["end"] - c["start"]
        c["enabled"] = c["confidence"] >= min_conf and used + length <= budget
        if c["enabled"]:
            used += length
    cuts.sort(key=lambda c: c["start"])

    chapters = _spaced([{"t": t(c["idx"]), "title": c["title"].strip()[:60]} for r in results
                        for c in r.get("chapters", []) if t(c["idx"]) is not None],
                       float(lf(cfg, "chapter_min_minutes", 4)) * 60)
    lower = _spaced([{"t": t(c["idx"]), "title": c["title"].strip()[:40], "subtitle": c["subtitle"].strip()[:70]}
                     for r in results for c in r.get("lower_thirds", []) if t(c["idx"]) is not None],
                    float(lf(cfg, "lower_third_min_gap_minutes", 6)) * 60)
    popups = _spaced([{"t": t(c["idx"]), "text": " ".join(c["text"].split()[:7]), "enabled": True}
                      for r in results for c in r.get("popups", []) if t(c["idx"]) is not None],
                     float(lf(cfg, "popup_min_gap_seconds", 90)))
    zooms = []
    for r in results:
        for z in r.get("zooms", []):
            s = by_idx.get(int(z["idx"]))
            if s:
                zooms.append({"t": s["start"], "end": min(s["end"], s["start"] + 6.0)})
    zooms = _spaced(zooms, float(lf(cfg, "zoom_min_gap_seconds", 25)))

    text = " ".join(s["text"] for s in inside)
    chapter_list = "\n".join(f"- {c['title']}" for c in chapters) or "(none)"
    meta = llm(
        f"You write YouTube metadata for {cfg.get('brand.name')}'s full livestream replays.",
        f"""Chapters:
{chapter_list}

Full transcript:
<transcript>
{text}
</transcript>

Write: a searchable, honest YouTube title under 70 characters (no clickbait that the video doesn't deliver);
a description of 2-3 short paragraphs saying what the stream covers and who it helps (no chapter list, no
links, no hashtags; those are added separately); and 8-15 tags.""",
        META_SCHEMA,
    )
    log.info("Plan: %d cuts (%d enabled, %.0fs), %d chapters, %d lower thirds, %d pop-ups, %d zooms",
             len(cuts), sum(c["enabled"] for c in cuts), used, len(chapters), len(lower), len(popups), len(zooms))
    return {"cuts": cuts, "chapters": chapters, "lower_thirds": lower, "popups": popups, "zooms": zooms,
            "meta": {"title": meta["title"][:100], "description": meta["description"],
                     "tags": [x.lstrip("#") for x in meta["tags"]][:20]}}


# =========================================================================== timeline

def keep_ranges(seg: dict, plan: dict, words: list[dict], tighten_over: float, keep_pause: float,
                fps: int) -> list[dict]:
    """Source ranges that stay in the video, snapped to whole frames. Each: {s, e, frames}."""
    starts = [w["s"] for w in words]
    removals: list[tuple[float, float]] = []
    for c in plan.get("cuts", []):
        if not c.get("enabled"):
            continue
        # From just before the first cut word to just before the next spoken word, so the edit is clean.
        nxt = bisect.bisect_left(starts, c["end"] - 0.01)
        end = (words[nxt]["s"] - 0.15) if nxt < len(words) else seg["end"]
        removals.append((max(seg["start"], c["start"] - 0.1), max(end, c["end"])))
    if tighten_over > 0:
        inside = [w for w in words if seg["start"] <= w["s"] <= seg["end"]]
        for a, b in zip(inside, inside[1:]):
            if b["s"] - a["e"] > tighten_over:
                removals.append((a["e"] + keep_pause / 2, b["s"] - keep_pause / 2))
    keep = []
    cursor = seg["start"]
    for a, b in _merge_ranges([(max(a, seg["start"]), min(b, seg["end"])) for a, b in removals if b > a]):
        if a - cursor >= 0.4:
            keep.append((cursor, a))
        cursor = max(cursor, b)
    if seg["end"] - cursor >= 0.4:
        keep.append((cursor, seg["end"]))
    out = []
    for s, e in keep:
        frames = max(1, round((e - s) * fps))
        out.append({"s": round(s, 4), "e": round(s + frames / fps, 4), "frames": frames})
    return out


class Timeline:
    """Maps source time to output time. Output time includes the intro."""

    def __init__(self, keep: list[dict], intro_frames: int, fps: int):
        self.keep, self.fps = keep, fps
        self.offsets = []
        f = intro_frames
        for k in keep:
            self.offsets.append(f / fps)
            f += k["frames"]
        self.end = f / fps
        self._starts = [k["s"] for k in keep]

    def map(self, t: float) -> float | None:
        i = bisect.bisect_right(self._starts, t) - 1
        if i >= 0 and t < self.keep[i]["e"]:
            return self.offsets[i] + (t - self.keep[i]["s"])
        if i + 1 < len(self.keep):
            return self.offsets[i + 1]  # inside a cut: snap to where the video resumes
        return None


def output_events(plan: dict, tl: Timeline, intro_s: float, cfg: Config) -> dict:
    chapters = []
    for c in plan.get("chapters", []):
        t = tl.map(c["t"])
        if t is not None and (not chapters or t - chapters[-1]["t"] >= 10):
            chapters.append({"t": t, "title": c["title"]})
    first_title = chapters[0]["title"] if chapters and chapters[0]["t"] - intro_s < 90 else "Intro"
    if chapters and chapters[0]["t"] - intro_s < 90:
        chapters.pop(0)
    chapters.insert(0, {"t": 0.0, "title": first_title})

    def mapped(items, key="t"):
        out = []
        for it in items:
            if it.get("enabled", True):
                t = tl.map(it[key])
                if t is not None:
                    out.append({**it, "t": t})
        return out

    zooms = []
    for z in plan.get("zooms", []):
        t = tl.map(z["t"])
        if t is not None:
            zooms.append({"t": t, "end": t + max(1.0, z["end"] - z["t"])})
    events = {"chapters": chapters, "lower_thirds": mapped(plan.get("lower_thirds", [])),
              "popups": mapped(plan.get("popups", [])), "zooms": zooms, "subscribe": None}
    if lf(cfg, "subscribe_reminder", True) and tl.end > 180:
        events["subscribe"] = round(intro_s + (tl.end - intro_s) / 3, 2)
    return events


def chapters_text(chapters: list[dict]) -> str:
    def stamp(t):
        t = int(t)
        return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"
    return "\n".join(f"{stamp(c['t'])} {c['title']}" for c in chapters)


def full_description(meta: dict, chapters: list[dict]) -> str:
    desc = meta.get("description", "").strip()
    if len(chapters) >= 3:  # YouTube only shows chapters with 3+ timestamps starting at 0:00
        desc += "\n\nChapters\n" + chapters_text(chapters)
    if meta.get("tags"):
        desc += "\n\n" + " ".join("#" + t.replace(" ", "") for t in meta["tags"][:3])
    return desc[:4900]


# =========================================================================== overlays

def _box_style(name, font, size, text, box, align, margin_v=0, outline=14):
    return (f"Style: {name},{font},{size},{text},{text},{box},{box},-1,0,0,0,100,100,0,0,3,{outline},0,"
            f"{align},0,0,{margin_v},1")


def build_overlay_ass(events: dict, brand: dict, w: int, h: int, total: float) -> str:
    font = brand.get("font", "Arial")
    white, black = ass_color("#FFFFFF"), ass_color("#000000")
    accent = ass_color(brand.get("accent_color", "#E63946"))
    hl = ass_color(brand.get("highlight_color", "#FFD400"))
    dark = ass_color("#111111", 40)
    sx = w / 1920
    styles = [
        _box_style("LTTitle", font, int(52 * sx), white, accent, 1),
        _box_style("LTSub", font, int(36 * sx), white, dark, 1, outline=12),
        _box_style("Pop", font, int(48 * sx), black, hl, 9),
        _box_style("Chap", font, int(80 * sx), white, dark, 5, outline=26),
        _box_style("ChapNum", font, int(34 * sx), white, accent, 5),
        _box_style("Subs", font, int(42 * sx), white, accent, 3),
    ]
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{chr(10).join(styles)}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    x0, y_title, y_sub = int(90 * sx), int(h - 150 * sx), int(h - 82 * sx)

    def add(start, dur, style, text, layer=1):
        end = min(total, start + dur)
        if end > start:
            ev.append(f"Dialogue: {layer},{_ts(start)},{_ts(end)},{style},,0,0,0,,{text}")

    for i, c in enumerate(events["chapters"][1:], start=2):
        t = c["t"]
        add(t, 3.4, "ChapNum", f"{{\\pos({w // 2},{int(h * 0.40)})\\fad(250,300)}}CHAPTER {i}")
        add(t, 3.4, "Chap", f"{{\\pos({w // 2},{h // 2})\\fad(250,300)\\fscx85\\fscy85\\t(0,350,\\fscx100\\fscy100)}}"
                            f"{_esc(c['title'].upper())}")
    for lt in events["lower_thirds"]:
        t = lt["t"] + 0.5
        add(t, 6.5, "LTTitle", f"{{\\move({-int(700 * sx)},{y_title},{x0},{y_title},0,450)\\fad(0,350)}}"
                               f"{_esc(lt['title'].upper())}", layer=2)
        add(t + 0.25, 6.25, "LTSub", f"{{\\move({-int(900 * sx)},{y_sub},{x0},{y_sub},0,450)\\fad(0,350)}}"
                                     f"{_esc(lt['subtitle'])}")
    xr, yt = int(w - 70 * sx), int(140 * sx)
    for p in events["popups"]:
        add(p["t"], 4.2, "Pop", f"{{\\move({w + int(500 * sx)},{yt},{xr},{yt},0,300)\\fad(0,300)"
                                f"\\t(300,450,\\fscx106\\fscy106)\\t(450,600,\\fscx100\\fscy100)}}{_esc(p['text'])}")
    if events.get("subscribe") is not None:
        t = events["subscribe"]
        add(t, 5.0, "Subs", f"{{\\move({w + int(400 * sx)},{int(h - 70 * sx)},{int(w - 70 * sx)},{int(h - 70 * sx)},0,400)"
                            f"\\fad(0,300)}}SUBSCRIBE FOR MORE ▶")
    return head + "\n".join(ev) + "\n"


def build_card_ass(kind: str, lines: list[str], brand: dict, w: int, h: int, dur: float) -> str:
    font = brand.get("font", "Arial")
    white = ass_color("#FFFFFF")
    accent = ass_color(brand.get("accent_color", "#E63946"))
    hl = ass_color(brand.get("highlight_color", "#FFD400"))
    sx = w / 1920
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Big,{font},{int(96 * sx)},{white},{white},{ass_color('#000000')},{ass_color('#000000', 160)},-1,0,0,0,100,100,2,0,1,0,6,5,60,60,0,1
Style: Small,{font},{int(46 * sx)},{hl},{hl},{ass_color('#000000')},{ass_color('#000000', 160)},-1,0,0,0,100,100,6,0,1,0,3,5,60,60,0,1
Style: Bar,{font},20,{accent},{accent},{accent},{accent},0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    end = _ts(dur)
    ev = []
    bar_w, bar_h = int(520 * sx), max(6, int(12 * sx))
    bx, by = w // 2 - bar_w // 2, int(h * 0.58)
    ev.append(f"Dialogue: 0,{_ts(0.2)},{end},Bar,,0,0,0,,{{\\pos({bx},{by})\\fscx0\\t(0,600,\\fscx100)\\p1}}"
              f"m 0 0 l {bar_w} 0 {bar_w} {bar_h} 0 {bar_h}{{\\p0}}")
    y = int(h * 0.44)
    for i, line in enumerate(lines[:3]):
        style = "Big" if i == 0 else "Small"
        yy = y if i == 0 else int(h * 0.66) + (i - 1) * int(70 * sx)
        start = 0.15 + i * 0.35
        ev.append(f"Dialogue: 1,{_ts(start)},{end},{style},,0,0,0,,{{\\pos({w // 2},{yy})\\fad(300,400)"
                  f"\\fscx80\\fscy80\\t(0,400,\\fscx100\\fscy100)}}{_esc(line.upper() if i == 0 else line)}")
    return head + "\n".join(ev) + "\n"


# =========================================================================== rendering

def measure_loudness(wav: Path) -> dict | None:
    proc = media.run([media._bin("ffmpeg"), "-hide_banner", "-nostats", "-i", str(wav), "-af",
                      "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"])
    text = proc.stderr
    try:
        return json.loads(text[text.rindex("{"): text.rindex("}") + 1])
    except ValueError:
        return None


def _loudnorm_filter(m: dict | None) -> str:
    base = "loudnorm=I=-14:TP=-1.5:LRA=11"
    if not m:
        return base
    return (f"{base}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
            f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")


class Renderer:
    def __init__(self, cfg: Config, workdir: Path, encoder: str):
        self.cfg, self.dir, self.encoder = cfg, workdir, encoder
        self.w, self.h = int(lf(cfg, "width", 1920)), int(lf(cfg, "height", 1080))
        self.fps = int(lf(cfg, "fps", 30))
        if SAMPLE_RATE % self.fps:
            raise ValueError("longform.fps must divide 48000 (e.g. 24, 25, 30, 60)")
        r = cfg.get("render") or {}
        self.venc = media.encoder_args(encoder, int(r.get("crf", 20)), int(r.get("nvenc_cq", 23)))
        brand = cfg.get("brand") or {}
        logo = cfg.resolve(brand["logo"]) if brand.get("logo") else None
        self.logo = logo if (lf(cfg, "watermark", True) and logo and logo.exists()) else None
        fonts = cfg.resolve(brand["fonts_dir"]) if brand.get("fonts_dir") else None
        self.fonts = fonts if fonts and fonts.is_dir() and any(p.suffix.lower() in (".ttf", ".otf")
                                                                 for p in fonts.iterdir()) else None
        self.brand = brand

    def _subs(self, name: str) -> str:
        s = f"subtitles={name}"
        return s + (f":fontsdir={filter_path(self.fonts)}" if self.fonts else "")

    def _fit(self) -> str:
        return (f"fps={self.fps},scale={self.w}:{self.h}:force_original_aspect_ratio=decrease,"
                f"pad={self.w}:{self.h}:(ow-iw)/2:(oh-ih)/2,setsar=1")

    def part(self, source: Path, k: dict, offset: float, zooms: list[dict], name: str, amount: float) -> tuple[Path, Path]:
        """Video-only .ts with exactly k['frames'] frames, and PCM audio with exactly the matching samples."""
        dur = k["frames"] / self.fps
        vout, aout = self.dir / f"{name}.ts", self.dir / f"{name}.wav"
        chain = [f"[0:v]{self._fit()},tpad=stop_mode=clone:stop_duration=1"]
        local = [(z["t"] - offset, z["end"] - offset) for z in zooms if z["end"] > offset and z["t"] < offset + dur]
        if local:
            ease = 0.3
            terms = "+".join(f"max(0,min(1,min((on/{self.fps}-({a:.3f}))/{ease},(({b:.3f})-on/{self.fps})/{ease})))"
                             for a, b in local)
            chain.append(f"zoompan=z='1+{amount}*min(1,{terms})':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                         f":d=1:s={self.w}x{self.h}:fps={self.fps}")
        graph = ",".join(chain) + "[base]"
        last = "base"
        inputs = ["-ss", f"{k['s']:.4f}", "-t", f"{dur + 0.5:.4f}", "-i", str(source)]
        if self.logo:
            inputs += ["-i", str(self.logo)]
            graph += (f";[1:v]scale={int(130 * self.w / 1920)}:-1,format=rgba,colorchannelmixer=aa=0.7[lg];"
                      f"[{last}][lg]overlay=W-w-{int(40 * self.w / 1920)}:{int(36 * self.w / 1920)}[branded]")
            last = "branded"
        graph += f";[{last}]setpts=PTS-STARTPTS+{offset:.4f}/TB,{self._subs('overlay.ass')},setpts=PTS-STARTPTS[v]"
        media.ffmpeg(*inputs, "-filter_complex", graph, "-map", "[v]", "-frames:v", str(k["frames"]), "-an",
                     *self.venc, "-f", "mpegts", vout.name, cwd=self.dir)
        n = k["frames"] * SAMPLE_RATE // self.fps
        fade = min(0.03, dur / 4)
        media.ffmpeg("-ss", f"{k['s']:.4f}", "-t", f"{dur + 0.5:.4f}", "-i", str(source), "-vn", "-af",
                     f"aresample={SAMPLE_RATE},aformat=sample_fmts=s16:channel_layouts=stereo,apad,"
                     f"atrim=end_sample={n},afade=t=in:d={fade:.3f},afade=t=out:st={dur - fade:.4f}:d={fade:.3f}",
                     "-c:a", "pcm_s16le", aout.name, cwd=self.dir)
        return vout, aout

    def card(self, kind: str, lines: list[str], seconds: float, name: str) -> tuple[Path, Path, int]:
        custom = self.cfg.resolve(f"brand/{kind}.mp4")
        vout, aout = self.dir / f"{name}.ts", self.dir / f"{name}.wav"
        if custom.exists():
            info = media.probe(custom)
            frames = max(1, round(info["duration"] * self.fps))
            media.ffmpeg("-i", str(custom), "-vf", f"{self._fit()},tpad=stop_mode=clone:stop_duration=1",
                         "-frames:v", str(frames), "-an", *self.venc, "-f", "mpegts", vout.name, cwd=self.dir)
            n = frames * SAMPLE_RATE // self.fps
            src = ["-i", str(custom)] if info["has_audio"] else ["-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo"]
            media.ffmpeg(*src, "-vn", "-af", f"aresample={SAMPLE_RATE},aformat=sample_fmts=s16:channel_layouts=stereo,"
                         f"apad,atrim=end_sample={n}", "-c:a", "pcm_s16le", aout.name, cwd=self.dir)
            return vout, aout, frames
        frames = max(1, round(seconds * self.fps))
        ass = self.dir / f"{name}.ass"
        ass.write_text(build_card_ass(kind, lines, self.brand, self.w, self.h, frames / self.fps), encoding="utf-8")
        bg = self.brand.get("card_background", "#0E0E10")
        inputs = ["-f", "lavfi", "-i", f"color=c={bg.replace('#', '0x')}:s={self.w}x{self.h}:r={self.fps}:d={frames / self.fps + 1}"]
        graph = "[0:v]format=yuv420p[bg]"
        last = "bg"
        logo = self.cfg.resolve(self.brand["logo"]) if self.brand.get("logo") else None
        if logo and logo.exists():
            inputs += ["-loop", "1", "-framerate", str(self.fps), "-t", f"{frames / self.fps + 1:.3f}", "-i", str(logo)]
            graph += (f";[1:v]scale={int(220 * self.w / 1920)}:-1,format=rgba,fade=t=in:st=0:d=0.5:alpha=1[lg];"
                      f"[{last}][lg]overlay=(W-w)/2:H*0.12[lgd]")
            last = "lgd"
        graph += f";[{last}]{self._subs(ass.name)}[v]"
        media.ffmpeg(*inputs, "-filter_complex", graph, "-map", "[v]", "-frames:v", str(frames), *self.venc,
                     "-f", "mpegts", vout.name, cwd=self.dir)
        n = frames * SAMPLE_RATE // self.fps
        media.ffmpeg("-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo", "-af", f"atrim=end_sample={n}",
                     "-c:a", "pcm_s16le", aout.name, cwd=self.dir)
        return vout, aout, frames


def render_longform(cfg: Config, source: Path, seg: dict, plan: dict, words: list[dict], out_path: Path,
                    encoder: str | None = None, work_root: Path | None = None) -> dict:
    encoder = encoder or media.pick_encoder(cfg.get("render.encoder", "auto"))
    work_root = work_root or cfg.subdir("work")
    with tempfile.TemporaryDirectory(prefix="kir_long_", dir=work_root) as tmp:
        r = Renderer(cfg, Path(tmp), encoder)
        keep = keep_ranges(seg, plan, words, float(lf(cfg, "tighten_pauses_over", 3.0)),
                           float(lf(cfg, "keep_pause", 0.8)), r.fps)
        if not keep:
            raise ValueError("Nothing left to render after cuts")
        title = seg.get("title") or ""
        intro_title = plan.get("meta", {}).get("title") or title or cfg.get("brand.name", "")
        intro_v, intro_a, intro_frames = r.card("intro", [intro_title, cfg.get("brand.name", "")],
                                                float(lf(cfg, "intro_seconds", 4)), "intro")
        tl = Timeline(keep, intro_frames, r.fps)
        intro_s = intro_frames / r.fps
        events = output_events(plan, tl, intro_s, cfg)
        (Path(tmp) / "overlay.ass").write_text(build_overlay_ass(events, r.brand, r.w, r.h, tl.end), encoding="utf-8")
        outro_lines = list(lf(cfg, "outro_lines", None) or ["Thanks for watching", "Subscribe for more",
                                                          cfg.get("brand.name", "")])
        outro_v, outro_a, outro_frames = r.card("outro", outro_lines, float(lf(cfg, "outro_seconds", 8)), "outro")

        vids, auds = [intro_v], [intro_a]
        amount = float(lf(cfg, "zoom_amount", 0.12))
        total = len(keep)
        for i, k in enumerate(keep):
            v, a = r.part(source, k, tl.offsets[i], events["zooms"], f"p{i:05d}", amount)
            vids.append(v)
            auds.append(a)
            if i % 25 == 0 or i == total - 1:
                log.info("Long-form render: part %d/%d (%.0f%%)", i + 1, total, 100 * (i + 1) / total)
        vids.append(outro_v)
        auds.append(outro_a)

        (Path(tmp) / "v.txt").write_text("".join(f"file '{p.name}'\n" for p in vids), encoding="utf-8")
        (Path(tmp) / "a.txt").write_text("".join(f"file '{p.name}'\n" for p in auds), encoding="utf-8")
        media.ffmpeg("-f", "concat", "-safe", "0", "-i", "v.txt", "-c", "copy", "video.mp4", cwd=Path(tmp))
        media.ffmpeg("-f", "concat", "-safe", "0", "-i", "a.txt", "-c", "copy", "audio.wav", cwd=Path(tmp))
        loud = measure_loudness(Path(tmp) / "audio.wav")
        media.ffmpeg("-i", "video.mp4", "-i", "audio.wav", "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                     "-af", f"{_loudnorm_filter(loud)},aresample={SAMPLE_RATE}", "-c:a", "aac",
                     "-b:a", cfg.get("render.audio_bitrate", "192k"), "-movflags", "+faststart", "final.mp4",
                     cwd=Path(tmp))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(Path(tmp) / "final.mp4"), out_path)

    total_s = tl.end + outro_frames / r.fps
    seg_len = seg["end"] - seg["start"]
    kept = sum(k["frames"] for k in keep) / r.fps
    info = {"duration": round(total_s, 2), "source_seconds": round(seg_len, 2), "kept_seconds": round(kept, 2),
            "removed_seconds": round(seg_len - kept, 2), "parts": len(keep), "chapters": events["chapters"],
            "encoder": encoder}
    log.info("Long-form rendered %s: %s from %s of source (%d parts, %.0fs removed)", out_path.name,
             fmt_ts(total_s), fmt_ts(seg_len), len(keep), seg_len - kept)
    return info


# =========================================================================== jobs

def build(cfg: Config, conn, lf_id: int, llm=None) -> int:
    """Plan (if needed) and render one segment, then put it in the review queue. Returns the clip id."""
    row = conn.execute("SELECT * FROM longform WHERE id=?", (lf_id,)).fetchone()
    stream = conn.execute("SELECT * FROM streams WHERE id=?", (row["stream_id"],)).fetchone()
    seg = {"start": row["start"], "end": row["end"], "title": row["title"], "idx": row["idx"]}
    try:
        transcript = json.loads((cfg.subdir("transcripts") / f"{stream['hash']}.json").read_text(encoding="utf-8"))
        words = transcript["words"]
        plan = json.loads(row["plan_json"]) if row["plan_json"] else None
        if row["replan"] or plan is None:
            db.update_longform(conn, lf_id, status="planning", error=None)
            if llm is None:
                from .llm import ClaudeJSON
                llm = ClaudeJSON(cfg)
            plan = plan_segment(cfg, llm, words, transcript.get("energy"), seg)
            db.update_longform(conn, lf_id, plan_json=plan, replan=0)
        db.update_longform(conn, lf_id, status="rendering", error=None)
        version = 1 + conn.execute("SELECT COUNT(*) FROM events WHERE kind='longform_rendered' AND ref=?",
                                   (str(lf_id),)).fetchone()[0]
        out = cfg.subdir("longform") / f"stream_{row['stream_id']:04d}" / f"part{row['idx']}_v{version}.mp4"
        info = render_longform(cfg, Path(stream["file_path"]), seg, plan, words, out)
        plan["render"] = info
        meta = plan.get("meta", {})
        title = meta.get("title") or seg["title"] or "Stream replay"
        desc = full_description(meta, info["chapters"])
        fields = {"title": title, "caption": desc, "hashtags": meta.get("tags", []), "hook": seg["title"],
                  "platform_captions": {"youtube": {"title": title, "caption": desc}}}
        clip_id = row["clip_id"]
        if clip_id and db.get_clip(conn, clip_id)["status"] in ("review", "rejected", "failed", "pending"):
            db.update_clip(conn, clip_id, file_path=str(out), status="review", start=seg["start"], end=seg["end"],
                           **fields)
        else:
            clip_id = db.insert_clip(conn, row["stream_id"], {"start": seg["start"], "end": seg["end"], "score": 0,
                                                              "reason": "long-form edit", **fields},
                                     "review", kind="longform")
            db.update_clip(conn, clip_id, file_path=str(out))
        db.update_longform(conn, lf_id, status="ready", plan_json=plan, clip_id=clip_id)
        db.log_event(conn, "longform_rendered", lf_id, {"file": str(out), **{k: v for k, v in info.items()
                                                                             if k != "chapters"}})
        notify(cfg, f"Long-form video ready: {title}",
               f"{fmt_ts(info['duration'])} long, {info['removed_seconds'] / 60:.1f} min removed, "
               f"{len(info['chapters'])} chapters. Review it in the dashboard's Long-form tab.")
        return clip_id
    except Exception as exc:
        log.exception("Long-form build %s failed", lf_id)
        db.update_longform(conn, lf_id, status="failed", error=str(exc)[:1000])
        notify(cfg, "Long-form edit failed", f"Stream {row['stream_id']} part {row['idx']}: {exc}", level="alert")
        raise


def worker_tick(cfg: Config, conn, llm=None) -> int | None:
    row = conn.execute("SELECT id FROM longform WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
    if not row:
        return None
    try:
        return build(cfg, conn, row["id"], llm=llm)
    except Exception:
        return None


def apply_edits(cfg: Config, conn, lf_id: int, changes: dict) -> bool:
    """Apply review-screen edits. Returns True if a re-render was queued."""
    row = conn.execute("SELECT * FROM longform WHERE id=?", (lf_id,)).fetchone()
    if row is None or not row["plan_json"]:
        raise ValueError("Nothing to edit yet")
    if row["clip_id"]:
        st = db.get_clip(conn, row["clip_id"])["status"]
        if st in ("approved", "scheduled", "posted"):
            raise ValueError("This video is already scheduled; cancel its post before editing")
    plan = json.loads(row["plan_json"])
    meta = plan.setdefault("meta", {})
    for key in ("title", "description"):
        if changes.get(key) is not None:
            meta[key] = str(changes[key])
    if changes.get("tags") is not None:
        meta["tags"] = [t.lstrip("#") for t in changes["tags"]]
    rerender = False
    for key, flag in (("cuts", "cuts_enabled"), ("popups", "popups_enabled")):
        for idx, enabled in (changes.get(flag) or {}).items():
            items = plan.get(key, [])
            i = int(idx)
            if 0 <= i < len(items) and bool(items[i].get("enabled", True)) != bool(enabled):
                items[i]["enabled"] = bool(enabled)
                rerender = True
    db.update_longform(conn, lf_id, plan_json=plan)
    if row["clip_id"] and plan.get("render"):
        title = meta.get("title") or row["title"]
        desc = full_description(meta, plan["render"]["chapters"])
        db.update_clip(conn, row["clip_id"], title=title, caption=desc, hashtags=meta.get("tags", []),
                       platform_captions={"youtube": {"title": title, "caption": desc}})
    if rerender or changes.get("rerender"):
        db.update_longform(conn, lf_id, status="queued", replan=1 if changes.get("replan") else 0)
        rerender = True
    db.log_event(conn, "longform_edited", lf_id, {k: v for k, v in changes.items() if k != "description"})
    return rerender
