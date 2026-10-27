"""Script studio: shoot-ready short-form scripts for NEW videos, in your voice, from the /virality skill.

Give it an idea (and optionally a winner from Niche research to model). Claude writes five hooks on five
different formulas and the beats after the hook; then everything is checked locally:
  - every line goes through the AI-tell cleanup; if structural tells remain, Claude rewrites once;
  - every hook is scored, and the highest one whose payoff LEADS opens the script;
  - every SAY line is timed at your own measured pace, so the timecodes are real;
  - the title + cover pair is checked, and unknown numbers stay as {{your number}} (never invented).

Jake's rules, kept: the hook leads with a concrete payoff, the next beat shows the proof, one CTA
(comment a keyword), never invent a number, copy the formula never the video, no em dashes.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
import threading
from pathlib import Path

from . import db
from .config import Config
from .virality import (WRITING_RULES, check_title, clean_text, formulas, human_score, say_seconds, score_hook,
                       timecode)

log = logging.getLogger("kir.studio")

PLACEHOLDER_RE = re.compile(r"\{\{[^}]+\}\}")
BEAT_LABELS = ("PROOF", "STEP", "REHOOK", "PAYOFF", "CTA")

HOOK = {"type": "object", "properties": {"formula": {"type": "string"}, "spoken": {"type": "string"},
                                         "on_screen": {"type": "string"}},
        "required": ["formula", "spoken", "on_screen"], "additionalProperties": False}
BEAT = {"type": "object", "properties": {"label": {"type": "string", "enum": list(BEAT_LABELS)},
                                         "say": {"type": "string"}, "screen": {"type": "string"},
                                         "visual": {"type": "string"}},
        "required": ["label", "say", "screen", "visual"], "additionalProperties": False}
SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "working_title": {"type": "string"},
        "why_it_works": {"type": "string"},
        "hooks": {"type": "array", "items": HOOK},
        "beats": {"type": "array", "items": BEAT},
        "keyword": {"type": "string"},
        "gets": {"type": "string"},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "cover": {"type": "string"},
        "title": {"type": "string"},
        "linkedin": {"type": "string"},
    },
    "required": ["working_title", "why_it_works", "hooks", "beats", "keyword", "gets", "caption", "hashtags",
                 "cover", "title", "linkedin"],
    "additionalProperties": False,
}
VOICE_SCHEMA = {"type": "object", "properties": {"voice_md": {"type": "string"}}, "required": ["voice_md"],
                "additionalProperties": False}


# --------------------------------------------------------------------------- profile files

def _file(cfg: Config, key: str, default: str) -> Path:
    return cfg.resolve(cfg.get(f"studio.{key}", default))


def read_profile(cfg: Config) -> dict:
    out = {}
    for key, default in (("voice", "brand/voice.md"), ("audience", "brand/audience.md")):
        p = _file(cfg, f"{key}_file", default)
        out[key] = p.read_text(encoding="utf-8") if p.exists() else ""
    return out


def save_profile(cfg: Config, voice: str | None = None, audience: str | None = None) -> None:
    for key, default, text in (("voice", "brand/voice.md", voice), ("audience", "brand/audience.md", audience)):
        if text is not None:
            p = _file(cfg, f"{key}_file", default)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text.replace("\r\n", "\n"), encoding="utf-8")


def _best_clip_texts(cfg: Config, conn, limit: int = 5) -> list[dict]:
    """Transcripts of your strongest clips: by real views when there are results, else by score."""
    rows = conn.execute(
        """SELECT c.id, c.start, c."end", c.score, s.hash,
                  (SELECT MAX(f.views) FROM performance f JOIN posts p ON p.id=f.post_id WHERE p.clip_id=c.id) AS views
           FROM clips c JOIN streams s ON s.id=c.stream_id
           WHERE c.kind='short' AND c.parent_clip_id IS NULL AND s.kind='stream'
           ORDER BY views IS NULL, views DESC, c.score DESC LIMIT 40""").fetchall()
    out, seen = [], set()
    for r in rows:
        path = cfg.subdir("transcripts") / f"{r['hash']}.json"
        if not path.exists():
            continue
        words = json.loads(path.read_text(encoding="utf-8")).get("words", [])
        inside = [w for w in words if r["start"] - 0.01 <= w["s"] and w["e"] <= r["end"] + 0.3]
        text = " ".join(w["w"] for w in inside).strip()
        if len(inside) < 20 or text[:60] in seen:
            continue
        seen.add(text[:60])
        out.append({"clip_id": r["id"], "views": r["views"], "score": r["score"], "words": inside, "text": text})
        if len(out) >= limit:
            break
    return out


def pace_of(words: list[dict]) -> float | None:
    """Words per minute over the time actually spent talking (pauses over 1 s don't count)."""
    if len(words) < 20:
        return None
    talk = sum(min(b["s"] - a["s"], 1.0) for a, b in zip(words, words[1:])) + (words[-1]["e"] - words[-1]["s"])
    return len(words) / talk * 60 if talk > 0 else None


def measured_wpm(cfg: Config, conn) -> float | None:
    paces = [p for p in (pace_of(c["words"]) for c in _best_clip_texts(cfg, conn, 8)) if p]
    return round(statistics.median(paces)) if paces else None


def profile_wpm(cfg: Config, conn, voice_text: str | None = None) -> float:
    """Pace for timing scripts: the voice file's "Pace: N wpm", else measured from your clips, else 165."""
    m = re.search(r"\*\*Pace:\*\*\s*(\d{2,3})", voice_text if voice_text is not None else read_profile(cfg)["voice"])
    if m and 90 <= int(m.group(1)) <= 260:
        return float(m.group(1))
    return float(measured_wpm(cfg, conn) or cfg.get("studio.wpm", 165))


def build_voice(cfg: Config, conn, llm, samples: str = "") -> str:
    """Write voice.md from your own clips' transcripts (plus any captions you paste). Saves and returns it."""
    clips = _best_clip_texts(cfg, conn, 5)
    if not clips and not samples.strip():
        raise ValueError("No clip transcripts yet. Process a stream first, or paste 3-5 of your captions.")
    wpm = measured_wpm(cfg, conn)
    template = read_profile(cfg)["voice"] or Path(__file__).resolve().parents[1].joinpath("brand/voice.md").read_text(encoding="utf-8")
    body = "\n\n".join(f"<clip id=\"{c['clip_id']}\" views=\"{c['views'] or 'unknown'}\">\n{c['text']}\n</clip>" for c in clips)
    result = llm(
        f"You capture how {cfg.get('brand.name', 'a creator')} really talks, from their own words, so scripts "
        "can be written in their voice.",
        f"""Here are transcripts of their best short clips (spoken, from livestreams) and some of their own captions.

{body}
{f'<captions>{samples.strip()}</captions>' if samples.strip() else ''}

Measured pace across these clips: {f'{wpm:.0f} wpm' if wpm else 'unknown'}.

Fill in the profile below. Rules: quote real lines verbatim (never invent quotes); signature phrases must
appear in two or more samples; leave "Proof I can say out loud" and "Off limits" exactly as they are in the
template (only the creator can fill those in) unless the samples state a concrete result in the creator's
own words, in which case list it with the clip it came from. Keep the markdown structure and headings.

<template>
{template}
</template>""",
        VOICE_SCHEMA)
    text, _ = clean_text(result["voice_md"].strip() + "\n")
    save_profile(cfg, voice=text)
    db.log_event(conn, "voice_built", None, {"clips": [c["clip_id"] for c in clips], "wpm": wpm})
    return text


# --------------------------------------------------------------------------- writing

def _formula_menu() -> str:
    return "\n".join(f"- {f['name']}{' (payoff first)' if f['payoff_first'] else ''}: e.g. \"{f['template']}\""
                     for f in formulas())


def _prompts(cfg: Config, idea: str, based_on: dict | None, profile: dict, wpm: float, notes: str = "") -> tuple[str, str]:
    brand = cfg.get("brand.name", "the creator")
    model_block = ""
    if based_on:
        model_block = f"""
Model the STRUCTURE of this post that is already working (copy its formula, shape, length and pacing,
never its words or footage):
<winner creator="{based_on.get('creator')}" views="{based_on.get('views')}" multiple_of_their_median="{based_on.get('multiple')}"
        formula="{based_on.get('formula')}">
{based_on.get('hook') or based_on.get('title')}
</winner>"""
    system = f"""You write shoot-ready short-form video scripts (Reels, TikTok, Shorts) for {brand}, in their own voice,
for their audience. You never invent a number, result, client or source: when a figure is missing from the
proof bank, write {{{{your number}}}} (or {{{{your result}}}}) so the creator fills it in.

<voice>
{profile['voice']}
</voice>

<audience>
{profile['audience']}
</audience>"""
    user = f"""Idea: {idea}
{model_block}
Write the script.

Hooks: five options, each on a DIFFERENT formula from this library, each with a spoken line (say-able in about
3 seconds) and a separate on-screen line of 6 words or fewer. Lead with the concrete payoff (money made or saved,
time saved, or a number) inside the first 8 spoken words whenever the proof bank has one.
<formulas>
{_formula_menu()}
</formulas>

Beats AFTER the hook (the best hook is placed first automatically): the first beat is PROOF (show the receipt,
result or number on screen), then STEP / REHOOK / PAYOFF beats, with a REHOOK line near the middle ("Here's the
part nobody does"), and CTA last. Each beat has SAY (spoken), SCREEN (on-screen text, 6 words or fewer) and VISUAL
(what the viewer sees: several shots separated by " / ", none held longer than about 1.5 seconds; show the noun on
the word it is said; real screens and real proof, no invented UI). 20 to 45 seconds total at {wpm:.0f} words per
minute unless the winner is clearly longer.

CTA: exactly one: "Comment KEYWORD and I'll send you <the thing>." The keyword is one word, said and shown.
Caption: line 1 is the same comment-keyword ask (under 125 characters), then 2 to 4 short lines with words people
search for. No links. Hashtags: 3 to 5 specific ones (never #fyp, #viral, #explorepage).
Cover: 2 to 4 words, different words from the title. Title: for Shorts/YouTube, under 60 characters.
LinkedIn: a short LinkedIn version (first line under 140 characters, 900 to 1,300 characters, blank lines between
short paragraphs, no links, at most 3 hashtags, ends with a question only this post could ask).
why_it_works: one sentence on the pattern you copied.
{notes}
{WRITING_RULES}"""
    return system, user


def _text_fields(raw: dict):
    """(path, text) for every piece of generated copy, so it can be cleaned and checked."""
    for i, h in enumerate(raw.get("hooks", [])):
        yield ("hooks", i, "spoken"), h["spoken"]
        yield ("hooks", i, "on_screen"), h["on_screen"]
    for i, b in enumerate(raw.get("beats", [])):
        for k in ("say", "screen", "visual"):
            yield ("beats", i, k), b[k]
    for k in ("caption", "cover", "title", "linkedin", "gets"):
        yield (k,), raw.get(k, "")


def _set(raw: dict, path: tuple, value: str) -> None:
    if len(path) == 1:
        raw[path[0]] = value
    else:
        raw[path[0]][path[1]][path[2]] = value


def clean_script(raw: dict) -> list[dict]:
    """Clean every field in place. Returns the structural flags still present (with the line they're in)."""
    flags = []
    for path, text in list(_text_fields(raw)):
        if not text:
            continue
        out, rep = clean_text(text)
        _set(raw, path, out)
        for f in rep["flags"]:
            flags.append({"where": ".".join(map(str, path)), "text": out[:160], **f})
    return flags


def assemble(raw: dict, wpm: float) -> dict:
    """Pick the hook, time every beat, run the checks. Returns the stored script dict."""
    hooks = []
    for h in raw["hooks"]:
        s = score_hook(h["spoken"]) or {"score": 0, "verdict": "WEAK", "payoff": "MISSING", "formula": "Unclassified"}
        hooks.append({**h, "score": s["score"], "verdict": s["verdict"], "payoff": s["payoff"],
                      "payoff_token": s.get("payoff_token"), "detected_formula": s["formula"]})
    # Shootable first (no {{placeholder}} left in it), then payoff leading, then the score.
    order = sorted(range(len(hooks)), key=lambda i: (bool(PLACEHOLDER_RE.search(hooks[i]["spoken"])),
                                                     hooks[i]["payoff"] != "LEADS", -hooks[i]["score"]))
    chosen = order[0] if order else None
    beats = []
    if chosen is not None:
        beats.append({"label": "HOOK", "say": hooks[chosen]["spoken"], "screen": hooks[chosen]["on_screen"],
                      "visual": "Face, close / the proof teased on screen"})
    beats += [dict(b) for b in raw["beats"]]
    t = 0.0
    for b in beats:
        dur = max(1.0, say_seconds(b["say"], wpm))
        b["start"], b["end"] = round(t, 1), round(t + dur, 1)
        b["timecode"] = f"{timecode(t)}-{timecode(t + dur)}"
        t += dur
    # Blanks that matter: the ones in what will actually be shot and posted (not in hooks that weren't picked).
    used = json.dumps([beats, raw.get("caption"), raw.get("cover"), raw.get("title"), raw.get("linkedin"), raw.get("gets")])
    placeholders = sorted(set(PLACEHOLDER_RE.findall(used)))
    spoken = " ".join(b["say"] for b in beats)
    return {
        "working_title": raw.get("working_title", ""), "why_it_works": raw.get("why_it_works", ""),
        "hooks": hooks, "chosen_hook": chosen, "beats": beats, "length": round(t, 1), "wpm": wpm,
        "keyword": raw.get("keyword", "").strip().upper(), "gets": raw.get("gets", ""),
        "caption": raw.get("caption", ""), "hashtags": [h if h.startswith("#") else f"#{h}" for h in raw.get("hashtags", [])][:5],
        "cover": raw.get("cover", ""), "title": raw.get("title", ""), "linkedin": raw.get("linkedin", ""),
        "placeholders": placeholders, "title_check": check_title(raw.get("title", ""), raw.get("cover") or None),
        "human": human_score(spoken) if len(spoken.split()) >= 40 else None,
    }


def write_script(cfg: Config, conn, script_id: int, llm=None) -> dict:
    row = conn.execute("SELECT * FROM scripts WHERE id=?", (script_id,)).fetchone()
    based_on = json.loads(row["based_on"]) if row["based_on"] else None
    profile = read_profile(cfg)
    wpm = profile_wpm(cfg, conn, profile["voice"])
    if llm is None:
        from .llm import ClaudeJSON
        llm = ClaudeJSON(cfg)
    system, user = _prompts(cfg, row["idea"], based_on, profile, wpm)
    raw = llm(system, user, SCRIPT_SCHEMA)
    flags = clean_script(raw)
    rewrites = 0
    if flags:
        # One rewrite pass for the structural tells the mechanical cleanup can't fix.
        notes = "\n\nA first draft had these problems. Rewrite those lines and keep everything else as close as possible:\n" + \
                "\n".join(f"- {f['where']}: \"{f['text']}\" -> {f['name']}: {f['fix']}" for f in flags) + \
                f"\n<draft>\n{json.dumps(raw)}\n</draft>"
        system, user = _prompts(cfg, row["idea"], based_on, profile, wpm, notes)
        raw = llm(system, user, SCRIPT_SCHEMA)
        flags = clean_script(raw)
        rewrites = 1
    script = assemble(raw, wpm)
    script.update({"flags": flags, "rewrites": rewrites, "based_on": based_on, "idea": row["idea"]})
    script["markdown"] = markdown(script)
    db.update_script(conn, script_id, status="ready", script_json=script, error=None)
    db.log_event(conn, "script_written", script_id, {"hook": script["hooks"][script["chosen_hook"]]["spoken"]
                                                    if script["chosen_hook"] is not None else None})
    return script


def start_script(cfg: Config, conn, idea: str, based_on: dict | None = None, llm=None, background: bool = True) -> int:
    idea = (idea or "").strip()
    if not idea and not based_on:
        raise ValueError("Write the idea in a sentence (what happened, to whom, what it cost or returned)")
    now = db.iso(db.utcnow())
    script_id = conn.execute("INSERT INTO scripts(idea, based_on, status, created_at, updated_at) VALUES (?,?,?,?,?)",
                             (idea or f"Rewrite this for my audience: {based_on.get('hook')}",
                              json.dumps(based_on) if based_on else None, "writing", now, now)).lastrowid

    def job():
        c = db.connect(cfg.db_path)
        try:
            write_script(cfg, c, script_id, llm)
        except Exception as exc:
            log.exception("Script %s failed", script_id)
            db.update_script(c, script_id, status="failed", error=str(exc)[:500])

    if background:
        threading.Thread(target=job, name=f"script-{script_id}", daemon=True).start()
    else:
        job()
    return script_id


# --------------------------------------------------------------------------- output

def markdown(s: dict) -> str:
    h = s["hooks"][s["chosen_hook"]] if s.get("chosen_hook") is not None else None
    based = s.get("based_on")
    lines = [f"# Script: {s['working_title'] or s['title']}", "",
             "- platform: Reels, TikTok, Shorts",
             f"- length: {s['length']}s at {s['wpm']:.0f} wpm",
             f"- formula: {h['formula']} (hook {h['score']:.0f} {h['verdict']}, payoff {h['payoff']})" if h else "- formula: -",
             f"- keyword: {s['keyword']}"]
    if based:
        lines.append(f"- based on: {based.get('creator')}, {based.get('views') or 0:,} views, "
                     f"{based.get('multiple')}x their own median (rewritten for your audience)")
    if s["placeholders"]:
        lines.append(f"- fill in before shooting: {', '.join(s['placeholders'])}")
    lines += ["", "## Hook options", "", "| score | formula | spoken | on screen |", "| --- | --- | --- | --- |"]
    for i, o in enumerate(s["hooks"]):
        star = " (chosen)" if i == s.get("chosen_hook") else ""
        lines.append(f"| {o['score']:.0f} {o['verdict']}{star} | {o['formula']} | {o['spoken']} | {o['on_screen']} |")
    lines += ["", "## Script", ""]
    for n, b in enumerate(s["beats"], 1):
        lines += [f"### {n}. {b['label']} ({b['timecode']})", f"SAY: {b['say']}", f"SCREEN: {b['screen']}",
                  f"VISUAL: {b['visual']}", ""]
    lines += ["## Caption", "", s["caption"], "", "## Hashtags", "", " ".join(s["hashtags"]), "",
              "## Cover", "", f"COVER: {s['cover']}", f"TITLE: {s['title']}", ""]
    if s.get("linkedin"):
        lines += ["## LinkedIn post", "", s["linkedin"], ""]
    return "\n".join(lines)


def list_scripts(conn, limit: int = 30) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM scripts ORDER BY id DESC LIMIT ?", (limit,)):
        d = dict(r)
        d["script"] = json.loads(d.pop("script_json") or "null")
        d["based_on"] = json.loads(d["based_on"]) if d["based_on"] else None
        out.append(d)
    return out
