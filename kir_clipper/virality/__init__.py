"""Copy tools from the master-skills /virality skill, wrapped for the clipper.

The modules in this folder come from Jake Schincariol's master-skills repo (MIT); each keeps its
origin header. They run on plain text, locally, with no API calls:

  hookscore   score a hook 0-100 on five checks, name its formula (26 in hooks.json), and say
              whether it leads with a concrete payoff (money or a number in the first 8 words)
  humanize    strip machine tells: invisible characters, em dashes, the slop lexicon (slop.json);
              structural tells ("it's not just X, it's Y") are flagged, never auto-rewritten
  detect      a five-check "does this read as written by a person" panel
  collect     normalise pasted / CSV / yt-dlp post lists (niche research)
  speech      how long a line takes to say at a given pace
  title       title + cover text pair check

The functions below are what the rest of the app uses. Licence: LICENSE-master-skills.txt (MIT), which
also covers the code ported into jumpcut.py, audio.py and stopmotion.py.
"""

from __future__ import annotations

from . import collect, detect, hookscore, humanize, speech, title

_LEX = None


def lexicon() -> dict:
    global _LEX
    if _LEX is None:
        _LEX = humanize.load_lexicon()
    return _LEX


# --------------------------------------------------------------------------- hooks

def score_hook(text: str) -> dict | None:
    """Compact hook analysis for storage and the dashboard. None for empty text."""
    text = (text or "").strip()
    if not text:
        return None
    a = hookscore.analyse(text)
    p = a["payoff"]
    return {
        "text": text,
        "score": a["score"],
        "verdict": a["verdict"],
        "formula": a["formula"]["name"],
        "payoff": p["status"],
        "payoff_token": p["token"].rstrip(",.;:") if p["token"] else None,
        "payoff_word": p["word"],
        "weakest": a["weakest"].lower(),
        "flags": a["flags"],
    }


def formula_name(text: str) -> str:
    formula, _ = hookscore.classify(text or "")
    return formula["name"] if formula else "Unclassified"


def formulas() -> list[dict]:
    return [{"id": f["id"], "name": f["name"], "payoff_first": bool(f.get("payoff_first")),
             "template": f.get("template") or f.get("example") or ""} for f, _ in hookscore.load_formulas()]


# --------------------------------------------------------------------------- AI tells

def clean_text(text: str) -> tuple[str, dict]:
    """Mechanical cleanup of machine tells. Returns (clean text, report).

    The report has "fixed" (how many artefacts were removed, with what) and "flags" (structural
    tells that need a human or a rewrite; the sentence-length check is skipped for short copy)."""
    if not text or not text.strip():
        return text or "", {"fixed": 0, "changes": [], "flags": []}
    trailing_newline = text.endswith("\n")
    out, rep = humanize.humanize(text, lexicon())
    out = out if trailing_newline else out.rstrip("\n")
    changes = ([f"{h['name']} x{h['count']}" for h in rep["typographic"]]
               + [f"{h['find']} -> {h['replace']}" for h in rep["lexical"]]
               + [f"{h['name']} x{h['count']}" for h in rep["invisible"]])
    fixed = sum(h["count"] for k in ("invisible", "typographic", "lexical") for h in rep[k])
    flags = [{"name": f["name"], "fix": f["fix"]} for f in rep["structures"]
             if not f["name"].startswith("Uniform sentence length") or len(text) > 400]
    return out, {"fixed": fixed, "changes": changes, "flags": flags}


def _merge(reports: list[dict]) -> dict:
    flags, seen = [], set()
    for r in reports:
        for f in r["flags"]:
            if f["name"] not in seen:
                seen.add(f["name"])
                flags.append(f)
    return {"fixed": sum(r["fixed"] for r in reports),
            "changes": [c for r in reports for c in r["changes"]], "flags": flags}


COPY_FIELDS = ("title", "caption", "hook_overlay", "hook_overlay_alt")


def clean_clip_copy(clip: dict) -> dict:
    """Clean every piece of copy Claude wrote for a clip, in place. Returns the merged report.

    The spoken hook (`hook`) is the creator's own words from the transcript, so it is left alone.
    Hashtags are left alone too."""
    reports = []
    for key in COPY_FIELDS:
        if isinstance(clip.get(key), str) and clip[key]:
            clip[key], rep = clean_text(clip[key])
            reports.append(rep)
    for copy in (clip.get("platform_captions") or {}).values():
        for key in ("title", "caption"):
            if isinstance(copy, dict) and isinstance(copy.get(key), str) and copy[key]:
                copy[key], rep = clean_text(copy[key])
                reports.append(rep)
    return _merge(reports)


def polish(clip: dict) -> dict:
    """Clean a clip's Claude-written copy in place and score its hooks. Returns the `checks` dict
    stored with the clip (shown on the dashboard card)."""
    report = clean_clip_copy(clip)
    return {**clip_checks(clip), "copy": report}


# Added to every prompt that writes copy, so most tells never get written in the first place.
WRITING_RULES = """Write like a person talking, not like marketing copy:
- No em dashes. Use a comma, a full stop, or a new sentence.
- Never use stock phrases such as "game changer", "let's dive in", "here's the thing", "unlock your potential",
  "in today's fast-paced world", "journey", "elevate", "leverage", "it's not just X, it's Y",
  "it's not about X, it's about Y", or a staged reveal like "The result?".
- When the clip has a concrete number, amount of money, or time saved, lead with it."""


def human_score(text: str) -> dict:
    results, overall, verdict = detect.run(text, lexicon())
    return {"score": round(overall, 1), "verdict": verdict,
            "checks": {k.lower(): {"score": round(v[0], 1), "detail": v[1]} for k, v in results.items()}}


def clip_checks(clip: dict) -> dict:
    """Hook scores for the on-screen hooks (A and B) and the spoken opening line."""
    return {"hook": score_hook(clip.get("hook_overlay") or clip.get("hook") or ""),
            "hook_alt": score_hook(clip.get("hook_overlay_alt") or ""),
            "spoken": score_hook(clip.get("hook") or "")}


# --------------------------------------------------------------------------- timing

def say_seconds(text: str, wpm: float = speech.DEFAULT_WPM) -> float:
    return round(speech.seconds(text or "", wpm), 1)


def timecode(sec: float) -> str:
    return speech.timecode(sec)


def check_title(title_text: str, cover: str | None = None) -> dict:
    return title.check(title_text, cover or None)



__all__ = ["score_hook", "formula_name", "formulas", "clean_text", "clean_clip_copy", "human_score",
           "clip_checks", "say_seconds", "timecode", "check_title", "collect", "speech"]
