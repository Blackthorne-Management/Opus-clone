"""Highlight finder: picks self-contained moments, scores them 0-99, and writes hooks/captions.

Pass 1  - the transcript is cut into overlapping windows; Claude proposes candidate clips as
          sentence ranges with a score, hook, and one-sentence reason.
Dedupe  - candidates overlapping more than `overlap_dedupe_ratio` keep the higher score.
Pass 2  - each finalist is re-read with the sentences around it; Claude moves the start to the
          hook and extends the end until the thought completes, re-scores it, and writes
          per-platform titles/captions.
Snap    - boundaries land on word edges with a little breathing room, never mid-word.
"""

from __future__ import annotations

import logging
import re
import statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import Config

log = logging.getLogger("kir.highlights")

SENTENCE_END = re.compile(r"[.!?…]['\")\]]*$")


# --------------------------------------------------------------------------- transcript shaping

def build_sentences(words: list[dict], energy: list[float] | None = None, pause: float = 0.7,
                    max_words: int = 40) -> list[dict]:
    sentences: list[dict] = []
    current: list[dict] = []

    def flush():
        if current:
            sentences.append({
                "i": len(sentences), "start": current[0]["s"], "end": current[-1]["e"],
                "w0": current[0]["_i"], "w1": current[-1]["_i"],
                "text": " ".join(w["w"] for w in current).strip(),
            })
            current.clear()

    for idx, w in enumerate(words):
        w = {**w, "_i": idx}
        if current and (w["s"] - current[-1]["e"] > pause or len(current) >= max_words):
            flush()
        current.append(w)
        if SENTENCE_END.search(w["w"]):
            flush()
    flush()

    if energy:
        med = statistics.median(energy) if energy else -30.0
        for s in sentences:
            seg = energy[int(s["start"]): max(int(s["start"]) + 1, int(s["end"]) + 1)]
            s["db"] = round(max(seg) - med, 1) if seg else 0.0
    return sentences


def fmt_ts(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:01d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def render_sentences(sentences: list[dict]) -> str:
    lines = []
    for s in sentences:
        loud = f" +{s['db']:.0f}dB" if s.get("db", 0) >= 6 else ""
        lines.append(f"[{s['i']} {fmt_ts(s['start'])}{loud}] {s['text']}")
    return "\n".join(lines)


def windows(sentences: list[dict], window_s: float, overlap_s: float) -> list[list[dict]]:
    if not sentences:
        return []
    out, start = [], sentences[0]["start"]
    last_end = sentences[-1]["end"]
    while True:
        end = start + window_s
        chunk = [s for s in sentences if s["start"] >= start and s["start"] < end]
        if chunk:
            out.append(chunk)
        if end >= last_end:
            break
        start = end - overlap_s
    return out


# --------------------------------------------------------------------------- geometry helpers

def overlap_ratio(a: dict, b: dict) -> float:
    inter = max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
    shorter = min(a["end"] - a["start"], b["end"] - b["start"])
    return inter / shorter if shorter > 0 else 0.0


def dedupe(clips: list[dict], ratio: float = 0.5) -> list[dict]:
    kept: list[dict] = []
    for clip in sorted(clips, key=lambda c: (-c["score"], c["start"])):
        if all(overlap_ratio(clip, k) <= ratio for k in kept):
            kept.append(clip)
    return kept


def snap(clip: dict, sentences: list[dict], words: list[dict], lead_in: float, tail: float) -> dict:
    """Convert sentence indices to times on word edges, with padding that never cuts a neighbour."""
    s0 = sentences[clip["start_idx"]]
    s1 = sentences[clip["end_idx"]]
    first_w, last_w = s0["w0"], s1["w1"]
    prev_end = words[first_w - 1]["e"] if first_w > 0 else 0.0
    next_start = words[last_w + 1]["s"] if last_w + 1 < len(words) else words[last_w]["e"] + tail
    start = max(prev_end + 0.02, words[first_w]["s"] - lead_in, 0.0)
    end = min(next_start - 0.05, words[last_w]["e"] + tail)
    end = max(end, words[last_w]["e"])
    return {**clip, "start": round(start, 3), "end": round(end, 3)}


# --------------------------------------------------------------------------- prompts & schemas

CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_idx": {"type": "integer"},
                    "end_idx": {"type": "integer"},
                    "score": {"type": "integer"},
                    "hook": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["start_idx", "end_idx", "score", "hook", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}

PLATFORM_COPY = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "caption": {"type": "string"}},
    "required": ["title", "caption"],
    "additionalProperties": False,
}

REFINE_SCHEMA = {
    "type": "object",
    "properties": {
        "start_idx": {"type": "integer"},
        "end_idx": {"type": "integer"},
        "score": {"type": "integer"},
        "hook": {"type": "string"},
        "hook_overlay": {"type": "string"},
        "hook_overlay_alt": {"type": "string"},
        "reason": {"type": "string"},
        "title": {"type": "string"},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "tiktok": PLATFORM_COPY,
        "instagram": PLATFORM_COPY,
        "youtube": PLATFORM_COPY,
        "facebook": PLATFORM_COPY,
        "x": PLATFORM_COPY,
    },
    "required": ["start_idx", "end_idx", "score", "hook", "hook_overlay", "hook_overlay_alt", "reason", "title", "caption",
                 "hashtags", "tiktok", "instagram", "youtube", "facebook", "x"],
    "additionalProperties": False,
}

PLATFORMS = ("tiktok", "instagram", "youtube", "facebook", "x")


def system_prompt(criteria: str, cfg: Config, lessons: str = "") -> str:
    lesson_block = f"""

What has actually worked for this audience (A/B tests of on-screen hooks on the same clip):
<hook_results>
{lessons}
</hook_results>
Lean toward the patterns that won.""" if lessons else ""
    return f"""You are the clip editor for {cfg.get('brand.name', 'the creator')}'s livestreams.
You read timestamped transcripts and pick moments that work as standalone vertical short-form clips
(TikTok, Instagram Reels, YouTube Shorts).

Transcript lines look like `[index h:mm:ss +NdB] text`. The index identifies the sentence; `+NdB`
marks sentences noticeably louder than the stream average (energy, laughter, emphasis).
Clips are given as an inclusive range of sentence indices.

Length rules: a clip must run between {cfg.get('highlights.min_seconds')} and {cfg.get('highlights.max_seconds')}
seconds; {cfg.get('highlights.preferred_min_seconds')} to {cfg.get('highlights.preferred_max_seconds')} seconds is ideal.

Scoring criteria from the creator (follow these closely):
<criteria>
{criteria}
</criteria>{lesson_block}"""


def pass1_prompt(chunk: list[dict]) -> str:
    return f"""Here is part of a stream transcript.

<transcript>
{render_sentences(chunk)}
</transcript>

Find every moment in this section that could stand alone as a short clip. For each one give the
first and last sentence index (inclusive), a 0-99 score using the criteria, the hook (the opening line
a viewer hears), and a one-sentence reason it works. Start each clip on its hook, not on filler.
Return an empty list if nothing in this section is worth clipping. Do not invent indices that are
not in the transcript."""


def pass2_prompt(context: list[dict], clip: dict) -> str:
    return f"""A candidate clip was picked from this part of the stream:

<transcript>
{render_sentences(context)}
</transcript>

Candidate: sentences {clip['start_idx']} to {clip['end_idx']}, provisional score {clip['score']}.
Hook: {clip['hook']}
Reason: {clip['reason']}

Review it as the final edit:
1. Start: move start_idx so the clip opens on the strongest hook (trim warm-up, never start mid-thought).
2. End: make sure the thought fully completes and the payoff lands. Extend end_idx if the ending is cut
   off; trim it if it trails into unrelated talk. Stay within the length rules.
3. Re-score 0-99 honestly after the fix.
4. hook_overlay: at most 7 words of on-screen text for the first 2 seconds (no hashtags, no emojis).
   hook_overlay_alt: a second on-screen hook for an A/B test, at most 7 words, taking a clearly different
   angle (e.g. a question vs a bold claim, a number vs an emotion). Both must be honest to the clip.
5. Write a short title and a general caption, 3-6 relevant hashtags (with #), and per-platform copy:
   - tiktok: title = short punchy line; caption = 1-2 short sentences plus hashtags.
   - youtube: title = searchable Shorts title under 90 characters; caption = 1-2 sentences + #shorts.
   - instagram: caption can be longer, 2-4 sentences, a question to drive comments, then hashtags.
   - facebook: conversational 1-3 sentences. x: under 240 characters including hashtags.
Only use sentence indices shown above."""


# --------------------------------------------------------------------------- main entry

def find_highlights(cfg: Config, transcript: dict, llm, lessons: str = "") -> list[dict]:
    words = transcript["words"]
    if not words:
        log.warning("Transcript has no words; nothing to clip")
        return []
    sentences = build_sentences(words, transcript.get("energy"))
    criteria = Path(cfg.resolve(cfg.get("highlights.criteria_file", "virality_criteria.md"))).read_text(encoding="utf-8")
    system = system_prompt(criteria, cfg, lessons)
    h = cfg.get("highlights")
    min_s, max_s = float(h["min_seconds"]), float(h["max_seconds"])
    cap = int(h["max_clips_per_stream"])
    ratio = float(h.get("overlap_dedupe_ratio", 0.5))
    n = len(sentences)

    def clamp_idx(c: dict) -> dict | None:
        a = max(0, min(n - 1, int(c["start_idx"])))
        b = max(0, min(n - 1, int(c["end_idx"])))
        if b < a:
            return None
        return {**c, "start_idx": a, "end_idx": b, "score": max(0, min(99, int(c["score"]))),
                "start": sentences[a]["start"], "end": sentences[b]["end"]}

    # Pass 1
    wins = windows(sentences, float(cfg.get("llm.window_minutes", 25)) * 60,
                   float(cfg.get("llm.window_overlap_minutes", 3)) * 60)
    log.info("Highlight pass 1: %d sentences in %d windows", n, len(wins))
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda w: llm(system, pass1_prompt(w), CANDIDATE_SCHEMA), wins))
    candidates = [c for r in results for c in (clamp_idx(x) for x in r.get("clips", [])) if c]
    # Allow some slack before refinement, which can trim or extend.
    candidates = [c for c in candidates if min_s * 0.6 <= c["end"] - c["start"] <= max_s * 1.5]
    finalists = dedupe(candidates, ratio)[: cap * 2]
    log.info("Pass 1 found %d candidates, %d finalists after dedupe", len(candidates), len(finalists))

    # Pass 2
    def refine(c: dict) -> dict | None:
        lo, hi = max(0, c["start_idx"] - 4), min(n, c["end_idx"] + 10)
        r = llm(system, pass2_prompt(sentences[lo:hi], c), REFINE_SCHEMA)
        r = clamp_idx(r)
        if r is None:
            return None
        r["platform_captions"] = {p: r.pop(p) for p in PLATFORMS if p in r}
        return r

    with ThreadPoolExecutor(max_workers=4) as pool:
        refined = [r for r in pool.map(refine, finalists) if r]

    lead_in = float(h.get("lead_in_seconds", 0.15))
    tail = float(h.get("tail_seconds", 0.45))
    final = []
    for r in refined:
        clip = snap(r, sentences, words, lead_in, tail)
        length = clip["end"] - clip["start"]
        if not (min_s <= length <= max_s):
            log.info("Dropping %.1fs clip at %s (outside %s-%ss)", length, fmt_ts(clip["start"]), min_s, max_s)
            continue
        final.append(clip)
    final = dedupe(final, ratio)[:cap]
    final.sort(key=lambda c: c["start"])
    log.info("Highlight finder kept %d clips (cap %d). Scores: %s", len(final), cap,
             sorted((c["score"] for c in final), reverse=True))
    return final


def clip_words(words: list[dict], start: float, end: float) -> list[dict]:
    """Words inside a clip, re-timed relative to the clip start (for captions)."""
    return [{**w, "s": round(w["s"] - start, 3), "e": round(w["e"] - start, 3)}
            for w in words if w["s"] >= start - 0.01 and w["e"] <= end + 0.3]
