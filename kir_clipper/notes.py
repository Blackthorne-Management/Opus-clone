"""Notes on a clip in plain words ("captions a bit bigger", "start on 'nobody tells you'", "put 'money' behind
my head"), turned into settings and re-rendered, like giving notes to an editor.

Claude reads the note with the clip's current settings and transcript and answers with the exact changes it
can make (only real settings; anything it can't do, it says so). Notes that are a general preference
("captions are always too low") are offered as a rule: saved in your style, they apply to every new clip.
Every note and what it changed is kept in the `notes` table.
"""

from __future__ import annotations

import json
import logging

from . import db, style
from .config import Config

log = logging.getLogger("kir.notes")

NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "changes": {"type": "array", "items": {
            "type": "object",
            "properties": {"setting": {"type": "string", "enum": list(style.KNOBS)}, "value": {"type": "string"}},
            "required": ["setting", "value"], "additionalProperties": False}},
        "start_shift": {"type": "number"},
        "end_shift": {"type": "number"},
        "hook_text": {"type": "string"},
        "behind_text": {"type": "string"},
        "general": {"type": "boolean"},
        "rule_text": {"type": "string"},
        "cannot_do": {"type": "string"},
    },
    "required": ["reply", "changes", "start_shift", "end_shift", "hook_text", "behind_text", "general", "rule_text",
                 "cannot_do"],
    "additionalProperties": False,
}


def _knob_help() -> str:
    lines = []
    for k, (typ, rule, label) in style.KNOBS.items():
        allowed = (f"one of {', '.join(map(str, rule))}" if isinstance(rule, tuple) and typ is str
                   else f"{rule[0]} to {rule[1]}" if isinstance(rule, tuple) else "true/false" if typ is bool
                   else "a colour like #FFD400")
        lines.append(f"- {k}: {label} ({allowed})")
    return "\n".join(lines)


def interpret(cfg: Config, clip: dict, words: list[dict], note: str, llm) -> dict:
    recipe = clip.get("recipe") or {}
    current = {k: recipe.get(k) for k in style.KNOBS}
    transcript = " ".join(f"[{w['s']:.1f}] {w['w']}" for w in words[:400])
    result = llm(
        "You are the editor on a short-form clip. The creator gives you notes in plain words; you turn each note "
        "into exact setting changes. Only use the settings listed. Never invent a setting.",
        f"""The creator's note: "{note}"

Current settings:
{json.dumps(current, indent=1)}

Settings you can change:
{_knob_help()}

Other things you can do: move the start (start_shift, seconds; positive = start later) or the end (end_shift; negative
= end earlier) using the transcript times; replace the on-screen hook text (hook_text, at most 7 words; "" = keep);
choose the word to put behind their head (behind_text, a word they actually say; "" = keep). Use 0 / "" for anything
the note doesn't ask for.

Transcript of the clip (seconds from the clip's start, before pauses are cut):
{transcript or "(no words)"}

"general": true only when the note is a preference that should apply to every clip ("captions are always too low",
"I like bigger text"), not a fix for this clip ("start later", "use the word 'money'"). If general, write rule_text
as a short rule in plain words. If part of the note can't be done with these settings, say what in cannot_do.
`reply`: one plain sentence saying what you'll change.""",
        NOTE_SCHEMA)
    changes = style.clean({c["setting"]: c["value"] for c in result.get("changes", [])})
    return {**result, "changes": changes}


def apply_note(cfg: Config, conn, clip_id: int, note: str, llm) -> dict:
    """Interpret a note, re-render the clip with it, and record it. Returns what happened."""
    from .highlights import clip_words
    from .pipeline import edit_clip
    from .transcribe import transcribe
    from pathlib import Path

    note = (note or "").strip()
    if not note:
        raise ValueError("Write what you'd like changed")
    row = db.get_clip(conn, clip_id)
    if row is None:
        raise ValueError("That clip is gone")
    clip = db.clip_dict(row)
    if (clip.get("recipe") or {}).get("imported"):
        raise ValueError("This clip was posted as a finished file, so it can't be re-edited")
    stream = conn.execute("SELECT * FROM streams WHERE id=?", (clip["stream_id"],)).fetchone()
    transcript = transcribe(cfg, Path(stream["file_path"]), stream["hash"], float(stream["duration"] or 0))
    words = clip_words(transcript["words"], clip["start"], clip["end"])
    plan = interpret(cfg, clip, words, note, llm)

    recipe_changes = dict(plan["changes"])
    if plan.get("hook_text", "").strip():
        recipe_changes["hook_text"] = plan["hook_text"].strip()[:80]
    if plan.get("behind_text", "").strip():
        recipe_changes.update({"behind_word": True, "behind_text": plan["behind_text"].strip()[:40]})
    edits = {"recipe": recipe_changes} if recipe_changes else {}
    if abs(plan.get("start_shift") or 0) > 0.01:
        edits["start"] = max(0.0, clip["start"] + float(plan["start_shift"]))
    if abs(plan.get("end_shift") or 0) > 0.01:
        edits["end"] = clip["end"] + float(plan["end_shift"])
    rendered = False
    if edits:
        edit_clip(cfg, conn, clip_id, edits)
        rendered = True
    note_id = conn.execute(
        """INSERT INTO notes(clip_id, note, reply, changes, general, rule_text, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (clip_id, note, plan["reply"], json.dumps({**recipe_changes, **{k: edits[k] for k in ("start", "end") if k in edits}}),
         int(bool(plan.get("general") and plan["changes"])), plan.get("rule_text") or "", db.iso(db.utcnow()))).lastrowid
    db.log_event(conn, "clip_note", clip_id, {"note": note, "changes": recipe_changes})
    return {"id": note_id, "reply": plan["reply"], "changes": recipe_changes, "rendered": rendered,
            "cannot_do": plan.get("cannot_do") or "",
            "offer_rule": bool(plan.get("general") and plan["changes"]),
            "rule_text": plan.get("rule_text") or note}


def save_rule(cfg: Config, conn, note_id: int) -> dict:
    row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    if row is None:
        raise ValueError("That note is gone")
    changes = {k: v for k, v in json.loads(row["changes"] or "{}").items() if k in style.KNOBS}
    rule = style.add_rule(cfg, row["rule_text"] or row["note"], changes)
    conn.execute("UPDATE notes SET saved_rule=1 WHERE id=?", (note_id,))
    db.log_event(conn, "style_rule_saved", note_id, rule)
    return rule


def recent(conn, clip_id: int, limit: int = 5) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM notes WHERE clip_id=? ORDER BY id DESC LIMIT ?", (clip_id, limit))]
