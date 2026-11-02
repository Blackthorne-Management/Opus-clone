"""Sales video (VSL): one video that sells one offer, built from the owner's own answers.

The shape is the classic sales-conversation outline:
  INTRO      promise (what watching gets them), pain (where they are now, in their words), proof (it has
             been done for people like them), plan (how the video is laid out), map (how long, what's next)
  METHOD     the owner's process end to end, with its name
  OBJECTION  3-5 of them, each in four moves: what they think -> why that's wrong -> what's actually
             true -> proof
  CTA        one action, a real reason to act now, what happens next, and an optional P.S.

The owner answers the questions in plain words; Claude only orders and tightens those answers into
short spoken lines. It never invents a result, review or number: anything missing becomes a
{{placeholder}} and is listed. Every line is then cleaned of AI tells, timed at the owner's pace and
checked (intro length, method length, an objection without proof, more than one call to action).

The script is filmed in one take (a mess-up is fine: pause and say the line again, the retake cut keeps
the last one), then dropped into Edit a video in sales mode, which adds the proof images on the lines
that name them. See editor.py.
"""

from __future__ import annotations

import json
import logging
import re
import threading

from . import db
from .config import Config
from .studio import PLACEHOLDER_RE, profile_wpm, read_profile
from .virality import WRITING_RULES, clean_text, say_seconds, timecode

log = logging.getLogger("kir.salesvideo")

BLOCKS = ("INTRO", "METHOD", "OBJECTION", "CTA")
BEATS = ("promise", "pain", "proof", "plan", "map", "method", "think", "wrong", "right", "objection_proof",
         "action", "urgency", "next", "ps")
BEAT_NAMES = {"promise": "Promise", "pain": "Pain", "proof": "Proof", "plan": "Plan", "map": "Road map",
              "method": "Method", "think": "What they think", "wrong": "Why that's wrong",
              "right": "What's actually true", "objection_proof": "Proof", "action": "Call to action",
              "urgency": "Why now", "next": "What happens next", "ps": "P.S."}
PLACEMENTS = {
    "landing": "on a landing page, above a 'book a call' button",
    "booked": "sent to people who just booked a call or asked for a quote, before the call",
    "checkout": "on a page where people buy directly, with no call",
}
LONG_LINE_WORDS = 18
INTRO_MAX = 90.0
METHOD_MAX = 180.0

LINE = {"type": "object",
        "properties": {"block": {"type": "string", "enum": list(BLOCKS)},
                       "beat": {"type": "string", "enum": list(BEATS)},
                       "objection": {"type": "integer"},
                       "say": {"type": "string"},
                       "show": {"type": "string"}},
        "required": ["block", "beat", "objection", "say", "show"], "additionalProperties": False}
SALES_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "big_idea": {"type": "string"},
                   "lines": {"type": "array", "items": LINE},
                   "missing": {"type": "array", "items": {"type": "string"}}},
    "required": ["title", "big_idea", "lines", "missing"],
    "additionalProperties": False,
}

FIELDS = ("offer", "audience", "not_for", "placement", "promise", "pain", "proof", "method_name", "method",
          "cta", "next_step", "urgency", "flaw")


def clean_intake(raw: dict) -> dict:
    """Keep the owner's answers as they wrote them, trimmed. Objections: up to 5 with 4 answers each."""
    out = {k: str(raw.get(k) or "").strip()[:3000] for k in FIELDS}
    if out["placement"] not in PLACEMENTS:
        out["placement"] = "landing"
    objections = []
    for o in (raw.get("objections") or [])[:5]:
        o = {k: str((o or {}).get(k) or "").strip()[:1000] for k in ("think", "wrong", "right", "proof")}
        if o["think"]:
            objections.append(o)
    out["objections"] = objections
    missing = [label for key, label in (("offer", "what you sell"), ("promise", "the promise"),
                                        ("pain", "the pain"), ("method", "how it works"), ("cta", "the call to action"))
               if not out[key]]
    if missing:
        raise ValueError("Answer these first: " + ", ".join(missing))
    if not objections:
        raise ValueError("Add at least one objection (what people think that stops them buying)")
    return out


def _prompt(cfg: Config, intake: dict, profile: dict, wpm: float, notes: str = "") -> tuple[str, str]:
    brand = cfg.get("brand.name", "the creator")
    objections = "\n".join(
        f"<objection n=\"{i}\">\nwhat they think: {o['think']}\nwhy that's wrong: {o['wrong'] or '(not given)'}\n"
        f"what's actually true: {o['right'] or '(not given)'}\nproof: {o['proof'] or '(not given)'}\n</objection>"
        for i, o in enumerate(intake["objections"], 1))
    system = f"""You turn {brand}'s own answers about an offer into a sales video they will say on camera, in their
own voice. You are an editor, not a writer: keep their words, phrases and examples, and only order, trim and join
them into short spoken lines. You never invent a result, review, client, number, guarantee or deadline. When an
answer is missing, write a {{{{placeholder}}}} naming what's needed (e.g. {{{{a client result with a number}}}})
and list it under `missing`.

<voice>
{profile['voice']}
</voice>"""
    user = f"""The offer: {intake['offer']}
Who it's for: {intake['audience'] or '(not given)'}. Who it's not for: {intake['not_for'] or '(not given)'}.
Where the video plays: {PLACEMENTS[intake['placement']]}.

Their answers:
<promise>{intake['promise']}</promise>
<pain>{intake['pain']}</pain>
<proof>{intake['proof'] or '(none given)'}</proof>
<method name="{intake['method_name'] or '(no name yet)'}">{intake['method']}</method>
{objections}
<call_to_action>{intake['cta']}</call_to_action>
<what_happens_next>{intake['next_step'] or '(not given)'}</what_happens_next>
<real_reason_to_act_now>{intake['urgency'] or '(none: do not make one up)'}</real_reason_to_act_now>
<honest_flaw>{intake['flaw'] or '(none)'}</honest_flaw>

Build the script as `lines`, in this order:
1. INTRO (under 90 seconds spoken): promise, pain, proof, plan, map. Open on who it's for and the promise (or the
   strongest proof, then the promise); never warm up. The pain uses their customers' words. The plan says how the
   video goes ("how it works, then the questions everyone asks"); the map says roughly how long. If there's an
   honest flaw, say it plainly before the claim it makes believable.
2. METHOD (60-90 seconds, 3 minutes at most): the process step by step, with its name. Several `method` lines.
3. OBJECTION, for each one in the order given: think, wrong, right, objection_proof (set `objection` to its number;
   0 for every other line). The proof line names the real result or example. If none was given, use a placeholder.
4. CTA: action (exactly one thing to do), urgency only if a real reason was given, next (what happens after), and
   an optional ps (a one-line recap or who this isn't for).

Each `say` is one spoken line: short sentences, everyday words, at most about 15 words, something they'd actually
say on a sales call. Never say production words out loud ("framework", "road map", "beat", "part one").
`show` is what's on screen for that line: "you, on camera" or the specific proof to cut to (e.g. "the before and
after photos of the Smith kitchen", "screenshot of the $4,200 month"). Every proof line names something to show.
`big_idea`: the one idea the whole video argues, as old way vs. their new way, in one sentence.
`title`: a plain name for this video. Timed at {wpm:.0f} words per minute.
{notes}
{WRITING_RULES}"""
    return system, user


def _clean(raw: dict) -> list[dict]:
    flags = []
    for i, line in enumerate(raw.get("lines", [])):
        for key in ("say", "show"):
            out, rep = clean_text(line.get(key) or "")
            line[key] = out
            if key == "say":
                for f in rep["flags"]:
                    flags.append({"where": f"line {i + 1}", "text": out[:160], **f})
    raw["big_idea"], _ = clean_text(raw.get("big_idea") or "")
    return flags


def assemble(raw: dict, intake: dict, wpm: float) -> dict:
    """Time every line, total each block, and list what to fix before filming."""
    lines, t = [], 0.0
    for line in raw["lines"]:
        dur = max(0.8, say_seconds(line["say"], wpm))
        lines.append({**line, "beat_name": BEAT_NAMES.get(line["beat"], line["beat"]),
                      "start": round(t, 1), "end": round(t + dur, 1), "timecode": f"{timecode(t)}-{timecode(t + dur)}"})
        t += dur + 0.4                         # a breath between lines when they're read in one take
    blocks = {}
    for line in lines:
        b = blocks.setdefault(line["block"], {"start": line["start"], "end": line["end"]})
        b["end"] = line["end"]
    for b in blocks.values():
        b["seconds"] = round(b["end"] - b["start"], 1)

    warnings = []
    intro = blocks.get("INTRO", {}).get("seconds", 0)
    if intro > INTRO_MAX:
        warnings.append(f"The intro runs {intro:.0f}s. Keep it under {INTRO_MAX:.0f}s: people decide in the first minute.")
    method = blocks.get("METHOD", {}).get("seconds", 0)
    if method > METHOD_MAX:
        warnings.append(f"The method runs {method:.0f}s. Keep it under {METHOD_MAX:.0f}s (60-90s is best).")
    for n in range(1, len(intake["objections"]) + 1):
        proof = [l for l in lines if l["objection"] == n and l["beat"] == "objection_proof"]
        if not proof or all(PLACEHOLDER_RE.search(l["say"] + l["show"]) for l in proof):
            warnings.append(f"Objection {n} has no real proof yet. Proof is what changes their mind: find one or drop it.")
    if sum(1 for l in lines if l["beat"] == "action") > 1:
        warnings.append("There's more than one call to action. Keep exactly one.")
    long_lines = [i + 1 for i, l in enumerate(lines) if len(l["say"].split()) > LONG_LINE_WORDS]
    if long_lines:
        warnings.append(f"Lines {', '.join(map(str, long_lines[:8]))} are long. Split them so they're easy to say.")
    if not any(l["beat"] in ("proof", "objection_proof") and not PLACEHOLDER_RE.search(l["say"]) for l in lines):
        warnings.append("There's no real proof anywhere yet. Add at least one result, review or before/after.")
    placeholders = sorted(set(PLACEHOLDER_RE.findall(json.dumps(lines))))
    return {"kind": "sales", "title": raw.get("title", ""), "big_idea": raw.get("big_idea", ""), "lines": lines,
            "blocks": blocks, "length": round(t, 1), "wpm": wpm, "warnings": warnings,
            "missing": [m for m in raw.get("missing", []) if m.strip()], "placeholders": placeholders,
            "proof_lines": [i for i, l in enumerate(lines) if l["beat"] in ("proof", "objection_proof")]}


def write(cfg: Config, conn, script_id: int, llm=None) -> dict:
    row = conn.execute("SELECT * FROM scripts WHERE id=?", (script_id,)).fetchone()
    intake = json.loads(row["based_on"])
    profile = read_profile(cfg)
    wpm = profile_wpm(cfg, conn, profile["voice"])
    if llm is None:
        from .llm import ClaudeJSON
        llm = ClaudeJSON(cfg)
    raw = llm(*_prompt(cfg, intake, profile, wpm), SALES_SCHEMA)
    flags = _clean(raw)
    rewrites = 0
    if flags:
        notes = ("\n\nA first draft had lines that sound machine-written. Rewrite only those, keeping the owner's words:\n"
                 + "\n".join(f"- {f['where']}: \"{f['text']}\" -> {f['fix']}" for f in flags)
                 + f"\n<draft>\n{json.dumps(raw)}\n</draft>")
        raw = llm(*_prompt(cfg, intake, profile, wpm, notes), SALES_SCHEMA)
        flags = _clean(raw)
        rewrites = 1
    script = assemble(raw, intake, wpm)
    script.update({"flags": flags, "rewrites": rewrites, "intake": intake})
    script["markdown"] = markdown(script)
    script["teleprompter"] = teleprompter(script)
    db.update_script(conn, script_id, status="ready", script_json=script, error=None)
    db.log_event(conn, "sales_script_written", script_id, {"lines": len(script["lines"]), "seconds": script["length"]})
    return script


def start(cfg: Config, conn, raw_intake: dict, llm=None, background: bool = True) -> int:
    intake = clean_intake(raw_intake)
    now = db.iso(db.utcnow())
    script_id = conn.execute(
        "INSERT INTO scripts(kind, idea, based_on, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        ("sales", f"Sales video: {intake['offer'][:120]}", json.dumps(intake), "writing", now, now)).lastrowid

    def job():
        c = db.connect(cfg.db_path)
        try:
            write(cfg, c, script_id, llm)
        except Exception as exc:
            log.exception("Sales script %s failed", script_id)
            db.update_script(c, script_id, status="failed", error=str(exc)[:500])

    if background:
        threading.Thread(target=job, name=f"sales-{script_id}", daemon=True).start()
    else:
        job()
    return script_id


# --------------------------------------------------------------------------- output

def markdown(s: dict) -> str:
    out = [f"# Sales video: {s['title']}", "", f"- the big idea: {s['big_idea']}",
           f"- length: about {s['length'] / 60:.1f} minutes at {s['wpm']:.0f} words per minute"]
    if s["placeholders"]:
        out.append(f"- fill in before filming: {', '.join(s['placeholders'])}")
    for w in s["warnings"]:
        out.append(f"- check: {w}")
    current = None
    for n, line in enumerate(s["lines"], 1):
        if line["block"] != current:
            current = line["block"]
            b = s["blocks"][current]
            out += ["", f"## {current.title()} ({b['seconds']:.0f}s)", ""]
        out.append(f"{n}. **{line['beat_name']}** ({line['timecode']})  ")
        out.append(f"   SAY: {line['say']}  ")
        out.append(f"   SHOW: {line['show']}")
    return "\n".join(out) + "\n"


def teleprompter(s: dict) -> str:
    """Just the lines, big gaps between them, for reading off a phone or laptop while filming."""
    head = ("Read each line, then pause for a breath. Mess one up? Pause and say it again: the edit keeps the last take.\n\n")
    return head + "\n\n".join(re.sub(r"\s+", " ", l["say"]).strip() for l in s["lines"]) + "\n"


def proof_slots(s: dict) -> list[dict]:
    """The lines a proof image can go on (used by Edit a video in sales mode)."""
    return [{"line": i, "say": s["lines"][i]["say"], "show": s["lines"][i]["show"]} for i in s.get("proof_lines", [])]
