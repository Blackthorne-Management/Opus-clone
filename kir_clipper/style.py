"""Your editing style: the settings every new clip starts from, plus the rules you've saved from notes.

Saved in brand/my-style.json (personal, not in git, so app updates never overwrite it):
  {"settings": {knob: value}, "rules": [{"id", "text", "changes", "created"}], "learned": {...}, "updated": ...}

`settings` come from "Learn my style from videos" or the My style tab; `rules` come from notes on clips you
chose to keep ("captions a bit bigger" -> caption_size 1.2 on every clip). Rules apply after settings, in the
order they were saved. Every value is checked against KNOBS before it is used, so a bad file can't break a render.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from .captions import CAPTION_STYLES
from .config import Config

PACES = ("tight", "normal", "loose", "off")
SFX_LEVELS = ("off", "light", "medium", "full")
ZOOMS = ("none", "punch", "slow_push")
CROPS = ("face", "center", "blur_pad")
FONTS = ("Montserrat Black", "Anton", "Arial", "Impact")
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")

# knob -> (type, allowed values or (min, max), what it means in plain words)
KNOBS: dict[str, tuple] = {
    "caption_style": (str, CAPTION_STYLES, "Caption look"),
    "caption_font": (str, FONTS, "Caption font"),
    "caption_size": (float, (0.6, 1.6), "Caption size (1 = normal)"),
    "caption_y": (float, (0.2, 0.85), "Where the bottom of the captions sits (share of the height from the top)"),
    "caption_words": (int, (1, 6), "Words shown at a time"),
    "primary_color": (str, "hex", "Caption colour"),
    "highlight_color": (str, "hex", "Colour of the word being spoken"),
    "emphasis_color": (str, "hex", "Colour of money, numbers and power words"),
    "caption_lead": (float, (-0.3, 0.3), "Captions earlier (+) or later (-), seconds"),
    "zoom": (str, ZOOMS, "Zoom"),
    "zoom_period": (float, (2.0, 12.0), "Seconds between punch-in zooms"),
    "zoom_amount": (float, (0.0, 0.25), "How far zooms push in"),
    "crop_mode": (str, CROPS, "Framing"),
    "jumpcut": (str, PACES, "Jump cuts"),
    "hook_overlay": (bool, None, "Hook text on screen for the first 2 seconds"),
    "outro_cta": (bool, None, "Call to action at the end"),
    "behind_word": (bool, None, "Hook word behind your head"),
    "sfx": (str, SFX_LEVELS, "Sound effects"),
}


def coerce(knob: str, value):
    """Validated value for one knob, or raise ValueError with a plain message."""
    if knob not in KNOBS:
        raise ValueError(f"'{knob}' isn't a style setting")
    typ, rule, label = KNOBS[knob]
    if typ is bool:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if typ in (int, float):
        try:
            v = typ(float(value))
        except (TypeError, ValueError):
            raise ValueError(f"{label}: must be a number")
        lo, hi = rule
        return min(hi, max(lo, v))
    v = str(value).strip()
    if rule == "hex":
        if not HEX.match(v):
            raise ValueError(f"{label}: use a colour like #FFD400")
        return v.upper()
    if v not in rule:
        raise ValueError(f"{label}: choose one of {', '.join(rule)}")
    return v


def clean(changes: dict) -> dict:
    """Keep only valid knob values (silently drops anything unknown or broken)."""
    out = {}
    for k, v in (changes or {}).items():
        try:
            out[k] = coerce(k, v)
        except ValueError:
            continue
    return out


def path(cfg: Config) -> Path:
    return cfg.resolve(cfg.get("style.file", "brand/my-style.json"))


def load(cfg: Config) -> dict:
    p = path(cfg)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("settings", {})
    data.setdefault("rules", [])
    return data


def save(cfg: Config, data: dict) -> None:
    from . import db
    data["updated"] = db.iso(db.utcnow())
    p = path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def effective(cfg: Config) -> dict:
    """Settings with every saved rule applied on top, validated."""
    data = load(cfg)
    out = clean(data["settings"])
    for rule in data["rules"]:
        out.update(clean(rule.get("changes")))
    return out


def apply(cfg: Config, recipe: dict) -> dict:
    """Put your style on a fresh recipe (called by render.default_recipe)."""
    recipe.update(effective(cfg))
    return recipe


def set_settings(cfg: Config, changes: dict, replace: bool = False, learned: dict | None = None) -> dict:
    data = load(cfg)
    new = {}
    for k, v in (changes or {}).items():
        if v in (None, ""):
            continue
        new[k] = coerce(k, v)            # raises with a plain message on a bad value
    data["settings"] = new if replace else {**clean(data["settings"]), **new}
    if learned is not None:
        data["learned"] = learned
    save(cfg, data)
    return data


def add_rule(cfg: Config, text: str, changes: dict) -> dict:
    changes = clean(changes)
    if not changes:
        raise ValueError("That note didn't change a style setting, so there's nothing to save as a rule")
    from . import db
    data = load(cfg)
    rule = {"id": uuid.uuid4().hex[:8], "text": (text or "").strip()[:200], "changes": changes,
            "created": db.iso(db.utcnow())}
    data["rules"].append(rule)
    save(cfg, data)
    return rule


def remove_rule(cfg: Config, rule_id: str) -> bool:
    data = load(cfg)
    before = len(data["rules"])
    data["rules"] = [r for r in data["rules"] if r.get("id") != rule_id]
    save(cfg, data)
    return len(data["rules"]) < before


def suggestions(cfg: Config, conn, last: int = 12, min_count: int = 3) -> list[dict]:
    """Changes you keep making by hand: the same setting set to the same value on several recent re-renders,
    different from what your style says. Each is offered as "make this the default"."""
    current = {**_defaults(cfg), **effective(cfg)}
    counts: dict[tuple, int] = {}
    rows = conn.execute("SELECT detail FROM events WHERE kind='clip_rerendered' ORDER BY id DESC LIMIT ?", (last,))
    for r in rows:
        try:
            recipe = (json.loads(r["detail"] or "{}") or {}).get("recipe") or {}
        except ValueError:
            continue
        for k, v in clean(recipe).items():
            if current.get(k) != v:
                counts[(k, json.dumps(v))] = counts.get((k, json.dumps(v)), 0) + 1
    out = []
    for (k, v), n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if n >= min_count:
            out.append({"knob": k, "label": KNOBS[k][2], "value": json.loads(v), "count": n})
    return out


def _defaults(cfg: Config) -> dict:
    from .render import default_recipe
    return clean(default_recipe(cfg, {}, use_style=False))


# --------------------------------------------------------------------------- learn from reference videos

LEARN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "observations": {"type": "array", "items": {"type": "string"}},
        "settings": {
            "type": "object",
            "properties": {
                "caption_style": {"type": "string", "enum": list(CAPTION_STYLES)},
                "caption_font": {"type": "string", "enum": list(FONTS)},
                "caption_size": {"type": "number"},
                "caption_y": {"type": "number"},
                "caption_words": {"type": "integer"},
                "primary_color": {"type": "string"},
                "highlight_color": {"type": "string"},
                "emphasis_color": {"type": "string"},
                "zoom": {"type": "string", "enum": list(ZOOMS)},
                "zoom_period": {"type": "number"},
                "jumpcut": {"type": "string", "enum": list(PACES)},
                "hook_overlay": {"type": "boolean"},
                "behind_word": {"type": "boolean"},
                "sfx": {"type": "string", "enum": list(SFX_LEVELS)},
            },
            "required": ["caption_style", "caption_font", "caption_size", "caption_y", "caption_words",
                         "primary_color", "highlight_color", "emphasis_color", "zoom", "zoom_period", "jumpcut",
                         "hook_overlay", "behind_word", "sfx"],
            "additionalProperties": False,
        },
        "cannot_copy": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "observations", "settings", "cannot_copy"],
    "additionalProperties": False,
}


def measure(video: Path, frames: int = 6, width: int = 432) -> dict:
    """Numbers and stills from one reference video: length, cuts (scene changes) and evenly spaced frames,
    with an extra one in the first second (the hook)."""
    import base64
    import subprocess
    from . import media
    info = media.probe(video)
    dur = max(0.1, info["duration"])
    proc = subprocess.run([media._bin("ffmpeg"), "-hide_banner", "-nostats", "-i", str(video), "-an",
                           "-vf", "select='gt(scene,0.3)',showinfo", "-f", "null", "-"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=media._CREATE_NO_WINDOW)
    cuts = [float(m) for m in re.findall(r"pts_time:([\d.]+)", proc.stderr)]
    shots = [b - a for a, b in zip([0.0] + cuts, cuts + [dur]) if b - a > 0.05]
    stills = []
    for t in [min(0.6, dur * 0.1)] + [dur * (k + 0.5) / frames for k in range(frames)]:
        jpg = subprocess.run([media._bin("ffmpeg"), "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}",
                              "-i", str(video), "-frames:v", "1", "-vf", f"scale={width}:-2", "-f", "image2",
                              "-c:v", "mjpeg", "-q:v", "4", "-"], capture_output=True,
                             creationflags=media._CREATE_NO_WINDOW).stdout
        if jpg:
            stills.append({"t": round(t, 1), "jpeg": base64.b64encode(jpg).decode()})
    shots.sort()
    return {"name": Path(video).name, "seconds": round(dur, 1), "width": info["width"], "height": info["height"],
            "cuts": len(cuts), "median_shot": round(shots[len(shots) // 2], 2) if shots else round(dur, 2),
            "stills": stills}


def learn(cfg: Config, videos: list[Path], llm) -> dict:
    """Ask Claude to read the shared editing style off 3-5 reference videos. Returns a proposal (not saved)."""
    if not videos:
        raise ValueError("Add 3 to 5 videos whose editing you like")
    measured = [measure(v) for v in videos[:5]]
    content: list[dict] = []
    for m in measured:
        content.append({"type": "text", "text": f"Reference video \"{m['name']}\": {m['seconds']} s, {m['width']}x{m['height']}, "
                                               f"{m['cuts']} cuts, median shot {m['median_shot']} s. Frames follow "
                                               f"(seconds: {', '.join(str(s['t']) for s in m['stills'])})."})
        for s in m["stills"]:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": s["jpeg"]}})
    knobs = "\n".join(f"- {k}: {label}" + (f" (one of {', '.join(map(str, rule))})" if isinstance(rule, tuple) and typ is str
                                            else f" ({rule[0]}-{rule[1]})" if isinstance(rule, tuple) else "")
                      for k, (typ, rule, label) in KNOBS.items() if k in LEARN_SCHEMA["properties"]["settings"]["properties"])
    content.append({"type": "text", "text": f"""These are reference videos whose editing style the creator wants for their
own clips (talking-head livestream clips, 9:16). Work out what they SHARE, ignoring one-off moments, and map it onto the
clipper's settings below. Look closely at the captions: size relative to the frame, how many words at a time, where they
sit (caption_y = where the bottom of the caption block sits as a share of the height from the top: 0.5 is the middle,
0.73 is the usual lower third), uppercase or not, colours (hex) for normal words, the spoken word and key words, and the
font family closest to one available. caption_size 1.0 is the style's normal size (hormozi is big, minimal is small).
Pace: many cuts and short shots = jumpcut tight. zoom punch = quick punch-ins every few seconds (zoom_period seconds);
slow_push = slow drift in. behind_word = a big word placed behind the speaker's head. sfx = how much sound design
the edits seem to use (you can't hear them; judge from how busy the motion is, and say so in observations).

Available settings:
{knobs}

`observations`: 4-8 short, specific notes with real numbers ("captions sit about 70% down", "a cut every 1.4 s").
`cannot_copy`: things in these edits the clipper can't do (e.g. B-roll, stickers, emoji, 3D text), so the creator knows.
`summary`: one sentence. Never copy footage, branding or logos; only the editing choices."""})
    result = llm("You are a short-form video editor who reverse-engineers editing styles precisely.", content, LEARN_SCHEMA)
    result["settings"] = clean(result.get("settings") or {})
    result["videos"] = [{k: m[k] for k in ("name", "seconds", "cuts", "median_shot")} for m in measured]
    return result


def proposal_path(cfg: Config) -> Path:
    return cfg.data_dir / "style_proposal.json"


def start_learning(cfg: Config, videos: list[Path], links: list[str], llm, background: bool = True) -> None:
    """Download links (yt-dlp), learn, and leave the proposal in style_proposal.json for the My style tab."""
    import shutil
    import subprocess
    import sys
    import tempfile
    import threading
    from . import db

    def write(state: dict) -> None:
        proposal_path(cfg).write_text(json.dumps({**state, "at": db.iso(db.utcnow())}), encoding="utf-8")

    def job():
        tmp = Path(tempfile.mkdtemp(prefix="kir_style_"))
        try:
            files = list(videos)
            failed = []
            for i, link in enumerate(links):
                out = tmp / f"ref{i}.%(ext)s"
                proc = subprocess.run([sys.executable, "-m", "yt_dlp", "-f", "mp4/best", "-o", str(out), link],
                                      capture_output=True, text=True, timeout=300)
                got = sorted(tmp.glob(f"ref{i}.*"))
                if proc.returncode == 0 and got:
                    files.append(got[0])
                else:
                    failed.append(link)
            if not files:
                raise ValueError("None of the links could be downloaded (Instagram and TikTok often block this). "
                                 "Screen-record the videos and upload the files instead.")
            result = learn(cfg, files, llm)
            result["failed_links"] = failed
            write({"status": "ready", "proposal": result})
        except Exception as exc:
            write({"status": "failed", "error": str(exc)[:400]})
        finally:
            for v in videos:
                Path(v).unlink(missing_ok=True)
            shutil.rmtree(tmp, ignore_errors=True)

    write({"status": "working"})
    if background:
        threading.Thread(target=job, name="style-learn", daemon=True).start()
    else:
        job()


def read_proposal(cfg: Config) -> dict:
    try:
        return json.loads(proposal_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def accept_proposal(cfg: Config, settings: dict | None = None) -> dict:
    prop = read_proposal(cfg)
    if prop.get("status") != "ready":
        raise ValueError("There's no learned style waiting to be saved")
    chosen = clean(settings) if settings else prop["proposal"]["settings"]
    data = set_settings(cfg, chosen, learned={k: prop["proposal"].get(k) for k in ("summary", "observations", "cannot_copy", "videos")})
    proposal_path(cfg).unlink(missing_ok=True)
    return data
