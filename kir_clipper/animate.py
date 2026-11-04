"""Animate an ad: a 15-60 second designed video (kinetic text, your screenshots and logo, music and
sound effects) made from a short brief, in 1-5 variations with different hooks.

How it works:
  1. Claude reads the brief and writes a storyboard per variation: a different hook angle each, then
     scenes (hook, problem, turn, reveal, benefits, proof, offer, cta), each with at most 7 words on
     screen. It only uses what the brief says: no invented results, reviews or numbers.
  2. This module turns each storyboard into a HyperFrames project (HTML + GSAP on a paused timeline,
     github.com/heygen-com/hyperframes, Apache-2.0) using a fixed set of scene designs in your colours
     and fonts, so every render works and looks the same each time.
  3. `npx hyperframes render` turns it into an MP4 (Node 22+ and the browser it downloads once). Music
     and whooshes/pops on the scene changes are mixed in afterwards and mastered like every clip.

Every ad keeps its project folder (data/ads/<id>/project), so it can be opened in HyperFrames' own
editor (`npx hyperframes preview`, the "Edit in HyperFrames" button) or reworked by Claude Code with
the HyperFrames skills.

Vertical ads keep every word inside the band the apps don't cover (168-1330 px of 1920) and put the hook
on frame 0, never a black or logo-only first frame.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from . import db, media
from .config import Config

log = logging.getLogger("kir.animate")

HYPERFRAMES = "hyperframes@0.9.13"         # pinned: the scene designs below are checked against this version
NODE_MIN = 22
KINDS = ("hook", "problem", "turn", "reveal", "benefits", "proof", "offer", "cta")
ASPECTS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080), "4:5": (1080, 1350)}
LENGTHS = (15, 20, 30, 45, 60)
MAX_WORDS = 7
MAX_IMAGES = 6
_lock = threading.Lock()

SCENE = {"type": "object",
         "properties": {"kind": {"type": "string", "enum": list(KINDS)},
                        "headline": {"type": "string"},
                        "emphasis": {"type": "string"},
                        "sub": {"type": "string"},
                        "items": {"type": "array", "items": {"type": "string"}},
                        "image": {"type": "integer"},
                        "seconds": {"type": "number"}},
         "required": ["kind", "headline", "emphasis", "sub", "items", "image", "seconds"],
         "additionalProperties": False}
STORYBOARD_SCHEMA = {
    "type": "object",
    "properties": {"variations": {"type": "array", "items": {
                       "type": "object",
                       "properties": {"angle": {"type": "string"}, "scenes": {"type": "array", "items": SCENE}},
                       "required": ["angle", "scenes"], "additionalProperties": False}},
                   "missing": {"type": "array", "items": {"type": "string"}}},
    "required": ["variations", "missing"],
    "additionalProperties": False,
}
FIELDS = ("product", "offer", "audience", "pain", "benefits", "proof", "cta", "handle", "notes")


# --------------------------------------------------------------------------- tools on this PC

def node_version() -> int | None:
    node = shutil.which("node")
    if not node:
        return None
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=20,
                             creationflags=media._CREATE_NO_WINDOW).stdout.strip()
        return int(out.lstrip("v").split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def ready() -> tuple[bool, str]:
    """(can render, why not in plain words)."""
    v = node_version()
    if v is None:
        return False, "Node.js isn't installed. Run Install.bat again (it adds it)."
    if v < NODE_MIN:
        return False, f"Node.js {v} is too old (needs {NODE_MIN} or newer). Run Install.bat again."
    if not shutil.which("npx"):
        return False, "npx (part of Node.js) wasn't found. Run Install.bat again."
    return True, ""


def _env() -> dict:
    return {**os.environ, "HYPERFRAMES_NO_TELEMETRY": "1", "DO_NOT_TRACK": "1", "HYPERFRAMES_SKIP_SKILLS": "1",
            "NO_COLOR": "1", "CI": "1"}


def hyperframes(args: list[str], cwd: Path, timeout: float = 900) -> subprocess.CompletedProcess:
    proc = subprocess.run([shutil.which("npx") or "npx", "--yes", HYPERFRAMES, *args], cwd=cwd, env=_env(),
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=media._CREATE_NO_WINDOW)
    if proc.returncode != 0:
        tail = ((proc.stderr or "") + (proc.stdout or ""))[-1500:]
        raise RuntimeError(f"HyperFrames {args[0]} failed: {tail}")
    return proc


# --------------------------------------------------------------------------- brief and storyboard

def clean_brief(raw: dict) -> dict:
    out = {k: str(raw.get(k) or "").strip()[:2000] for k in FIELDS}
    if not out["product"]:
        raise ValueError("Say what the ad is for (the product or offer)")
    if not out["cta"]:
        raise ValueError("Say what people should do at the end (the call to action)")
    try:
        length = int(raw.get("length") or 30)
    except (TypeError, ValueError):
        length = 30
    out["length"] = min(LENGTHS, key=lambda x: abs(x - length))
    out["aspect"] = raw.get("aspect") if raw.get("aspect") in ASPECTS else "9:16"
    try:
        out["variations"] = max(1, min(5, int(raw.get("variations") or 3)))
    except (TypeError, ValueError):
        out["variations"] = 3
    out["ground"] = raw.get("ground") if raw.get("ground") in ("dark", "light") else "dark"
    out["sfx"] = raw.get("sfx") if raw.get("sfx") in ("off", "light", "full") else "light"
    return out


def _prompt(brief: dict, images: list[str]) -> tuple[str, str]:
    from .virality import WRITING_RULES
    pics = "\n".join(f"  {i}: {name}" for i, name in enumerate(images)) or "  (none)"
    n_scenes = {15: "4-5", 20: "5-6", 30: "6-8", 45: "8-10", 60: "9-12"}[brief["length"]]
    system = ("You storyboard short animated video ads for social feeds (Reels, TikTok, Shorts, Meta ads). Viewers "
              "decide in one second and most watch muted, so the words on screen carry everything. You use only "
              "the facts in the brief: never invent a result, number, review, price, guarantee or feature.")
    user = f"""Brief (the owner's own words):
{json.dumps({k: brief[k] for k in FIELDS}, indent=1)}

Images they uploaded (use the number in `image`, -1 for none):
{pics}

Write {brief['variations']} variation(s) of a {brief['length']}-second ad. Each variation has a different `angle` (the
hook idea in a few words: a pain call-out, a bold claim from the brief, a question, a before/after, a "stop doing X")
and {n_scenes} scenes. Scene kinds, in this order where they fit:
- hook: the first second. A bold line that stops the scroll. Always first.
- problem: the pain in the audience's words.
- turn: the switch ("there's a faster way").
- reveal: the product by name; use the logo or a product image if there is one.
- benefits: up to 3 short `items` (3-5 words each), what they get.
- proof: only if the brief gives real proof; quote it exactly. Skip it otherwise.
- offer: what they get / the price / the deadline, only as the brief states it.
- cta: the call to action exactly as the brief says it; `sub` = the handle or link if given. Always last.

Rules for every scene:
- `headline`: at most {MAX_WORDS} words, punchy, no ending period. `emphasis`: the one word in it to colour ("" if none).
- `sub`: at most 12 words or "". `items`: only for benefits, else [].
- `image`: an uploaded image that shows what the scene says (a real screenshot or the logo), else -1. Never
  use one image for a claim it doesn't show.
- `seconds`: how long it stays up (1.5-6); the hook 1.5-3. Scenes add up to about {brief['length']} seconds.
{WRITING_RULES}
List anything the ad would be stronger with but the brief doesn't give (proof, a price, a deadline, a
screenshot of a named screen) in `missing`."""
    return system, user


def _words(s: str, limit: int) -> str:
    return " ".join(str(s or "").replace("\n", " ").split()[:limit])


def normalise(storyboard: dict, brief: dict, n_images: int) -> list[dict]:
    """Clean every line, keep the scene rules, and stretch the timings to exactly the asked length."""
    from .virality import clean_text as _clean

    def clean_text(t: str) -> str:
        return _clean(str(t or ""))[0]

    out = []
    for v in storyboard.get("variations", [])[: brief["variations"]]:
        scenes = []
        for s in v.get("scenes", []):
            if s.get("kind") not in KINDS:
                continue
            headline = _words(clean_text(s.get("headline", "")), MAX_WORDS).rstrip(".")
            if not headline:
                continue
            emph = s.get("emphasis", "").strip()
            if emph and emph.lower() not in headline.lower():
                emph = ""
            img = s.get("image", -1)
            scenes.append({"kind": s["kind"], "headline": headline, "emphasis": emph,
                           "sub": _words(clean_text(s.get("sub", "")), 14),
                           "items": [_words(clean_text(i), 6) for i in s.get("items", [])][:3] if s["kind"] == "benefits" else [],
                           "image": img if isinstance(img, int) and 0 <= img < n_images else -1,
                           "seconds": min(6.0, max(1.2, float(s.get("seconds") or 3)))})
        if not scenes:
            continue
        if scenes[0]["kind"] != "hook":
            scenes[0]["kind"] = "hook"
        if scenes[-1]["kind"] != "cta":
            scenes.append({"kind": "cta", "headline": _words(brief["cta"], MAX_WORDS), "emphasis": "",
                           "sub": _words(brief["handle"], 14), "items": [], "image": -1, "seconds": 3.0})
        scale = brief["length"] / sum(s["seconds"] for s in scenes)
        t = 0.0
        for s in scenes:
            s["seconds"] = round(s["seconds"] * scale, 2)
            s["start"] = round(t, 2)
            t += s["seconds"]
        scenes[-1]["seconds"] = round(brief["length"] - scenes[-1]["start"], 2)
        out.append({"angle": _words(v.get("angle", ""), 12), "scenes": scenes})
    if not out:
        raise ValueError("Claude didn't return any usable scenes. Try again with a little more in the brief.")
    return out


# --------------------------------------------------------------------------- the HyperFrames project

def palette(cfg: Config, ground: str) -> dict:
    from . import style
    s = style.effective(cfg)
    accent = s.get("highlight_color") or "#FFD400"
    if ground == "light":
        return {"ground": "#F4F1EA", "ground2": "#E6E0D2", "ink": "#111111", "muted": "#4A4A4A", "accent": accent,
                "on_accent": "#111111"}
    return {"ground": "#0B0B10", "ground2": "#1C1C26", "ink": s.get("primary_color") or "#FFFFFF", "muted": "#B9B9C6",
            "accent": accent, "on_accent": "#111111"}


def _headline_html(sid: str, text: str, emphasis: str) -> str:
    parts = []
    for i, word in enumerate(text.split()):
        cls = "w em" if emphasis and re.sub(r"\W", "", word).lower() == re.sub(r"\W", "", emphasis).lower() else "w"
        parts.append(f'<span class="{cls}" id="{sid}-w{i}">{html.escape(word)}</span>')
    return " ".join(parts)


def _count_target(text: str) -> tuple[str, float, str] | None:
    """('$', 312, 'k+ members') for a headline with a number in it, so the proof scene can count up to it."""
    m = re.search(r"([$€£]?)(\d[\d,]*(?:\.\d+)?)", text)
    if not m:
        return None
    return m.group(1), float(m.group(2).replace(",", "")), m.group(2)


def build_html(scenes: list[dict], size: tuple[int, int], pal: dict, images: list[str], length: float) -> str:
    W, H = size
    vertical = H > W * 1.3
    top, bottom = (168, 1330) if (W, H) == (1080, 1920) else (int(H * 0.08), int(H * 0.92))
    base = min(W, H)
    head_px = round(base * (0.13 if vertical else 0.095))
    sub_px = max(30, round(base * 0.042))
    item_px = max(30, round(base * 0.055))
    css = f"""
@font-face {{ font-family: "Display"; src: url("assets/fonts/Anton-Regular.ttf"); }}
@font-face {{ font-family: "Body"; src: url("assets/fonts/Montserrat-Black.ttf"); }}
:root {{ --ground: {pal['ground']}; --ground2: {pal['ground2']}; --ink: {pal['ink']}; --muted: {pal['muted']};
        --accent: {pal['accent']}; --on-accent: {pal['on_accent']}; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ width: {W}px; height: {H}px; overflow: hidden; background: var(--ground); }}
#root {{ position: relative; width: {W}px; height: {H}px; overflow: hidden; background: var(--ground); }}
#glow {{ position: absolute; width: {round(base * 1.3)}px; height: {round(base * 1.3)}px; border-radius: 50%;
        left: {round(W / 2 - base * 0.65)}px; top: {round(H / 2 - base * 0.65)}px; opacity: 0.55;
        background: radial-gradient(circle, var(--ground2) 0%, rgba(0,0,0,0) 65%); }}
.scene {{ position: absolute; left: 0; top: {top}px; width: {W}px; height: {bottom - top}px; display: flex;
         flex-direction: column; align-items: center; justify-content: center; gap: {round(base * 0.035)}px;
         padding: 0 {round(W * 0.08)}px; text-align: center; }}
.inner {{ display: flex; flex-direction: column; align-items: center; gap: {round(base * 0.035)}px; width: 100%; }}
.head {{ font-family: "Display", sans-serif; font-size: {head_px}px; line-height: 1.02; color: var(--ink);
        text-transform: uppercase; letter-spacing: 0.01em; max-width: {round(W * 0.84)}px; }}
.w {{ display: inline-block; }}
.em {{ color: var(--accent); }}
.sub {{ font-family: "Body", sans-serif; font-size: {sub_px}px; line-height: 1.25; color: var(--muted);
       max-width: {round(W * 0.8)}px; }}
.pic {{ max-width: {round(W * 0.78)}px; max-height: {round((bottom - top) * 0.5)}px; border-radius: {round(base * 0.03)}px;
       object-fit: contain; box-shadow: 0 {round(base * 0.02)}px {round(base * 0.06)}px rgba(0,0,0,0.45); }}
.items {{ display: flex; flex-direction: column; gap: {round(base * 0.028)}px; align-items: flex-start; }}
.item {{ display: flex; align-items: center; gap: {round(base * 0.03)}px; font-family: "Body", sans-serif;
        font-size: {item_px}px; color: var(--ink); text-align: left; }}
.tick {{ width: {round(item_px * 1.25)}px; height: {round(item_px * 1.25)}px; border-radius: 50%; background: var(--accent);
        color: var(--on-accent); display: flex; align-items: center; justify-content: center; font-size: {round(item_px * 0.8)}px;
        flex: none; }}
.bar {{ height: {max(8, round(base * 0.012))}px; width: {round(W * 0.42)}px; background: var(--accent); border-radius: 99px;
       transform-origin: left center; }}
.btn {{ font-family: "Display", sans-serif; font-size: {round(head_px * 0.62)}px; text-transform: uppercase;
       background: var(--accent); color: var(--on-accent); padding: {round(base * 0.03)}px {round(base * 0.07)}px;
       border-radius: 999px; }}
.count {{ font-family: "Display", sans-serif; font-size: {round(head_px * 1.9)}px; line-height: 1; color: var(--accent); }}
"""
    body, js = [], []
    exits = ["{y: -60, opacity: 0}", "{x: -120, opacity: 0}", "{scale: 0.9, opacity: 0}"]
    for n, s in enumerate(scenes):
        sid = f"s{n}"
        start, dur = s["start"], s["seconds"]
        last = n == len(scenes) - 1
        parts = []
        if s["kind"] == "proof" and (ct := _count_target(s["headline"])):
            prefix, value, shown = ct
            rest = s["headline"].replace(prefix + shown, "", 1).strip()
            parts.append(f'<div class="count" id="{sid}-count">{html.escape(prefix + shown)}</div>')
            if rest:
                parts.append(f'<div class="head" id="{sid}-head">{_headline_html(sid, rest, s["emphasis"])}</div>')
            decimals = len(shown.split(".")[1]) if "." in shown else 0
            js.append(f"""(function(){{ const el = document.getElementById("{sid}-count"); const o = {{v: 0}};
  tl.to(o, {{v: {value}, duration: {min(1.2, dur * 0.5):.2f}, ease: "power2.out", onUpdate: () => {{
    el.textContent = "{prefix}" + o.v.toLocaleString("en-US", {{minimumFractionDigits: {decimals}, maximumFractionDigits: {decimals}}}); }}}}, {start + 0.1:.2f});
  tl.fromTo(el, {{scale: 0.6, opacity: 0}}, {{scale: 1, opacity: 1, duration: 0.35, ease: "back.out(2)"}}, {start:.2f}); }})();""")
        else:
            parts.append(f'<div class="head" id="{sid}-head">{_headline_html(sid, s["headline"], s["emphasis"])}</div>')
        if s["kind"] in ("hook", "problem"):
            parts.append(f'<div class="bar" id="{sid}-bar"></div>')
        if s["image"] >= 0:
            parts.insert(0 if s["kind"] == "reveal" else len(parts),
                         f'<img class="pic" id="{sid}-pic" src="assets/{html.escape(images[s["image"]])}" alt="">')
        if s["items"]:
            rows = "".join(f'<div class="item" id="{sid}-i{k}"><span class="tick">✓</span><span>{html.escape(t)}</span></div>'
                           for k, t in enumerate(s["items"]))
            parts.append(f'<div class="items">{rows}</div>')
        if s["kind"] == "cta":
            parts = [f'<div class="head" id="{sid}-head">{_headline_html(sid, s["headline"], s["emphasis"])}</div>',
                     f'<div class="btn" id="{sid}-btn">{html.escape(_words(s["sub"], 5) or "Tap the link")}</div>']
            if s["image"] >= 0:
                parts.insert(0, f'<img class="pic" id="{sid}-pic" src="assets/{html.escape(images[s["image"]])}" alt="">')
        elif s["sub"]:
            parts.append(f'<div class="sub" id="{sid}-sub">{html.escape(s["sub"])}</div>')
        body.append(f'<div class="scene clip" id="{sid}" data-start="{start}" data-duration="{dur}" '
                    f'data-track-index="1" data-track-kind="graphics" style="z-index:{n + 2}">'
                    f'<div class="inner" id="{sid}-in">{"".join(parts)}</div></div>')

        words = len(re.findall(r'id="' + sid + r'-w\d+"', "".join(parts)))
        a = start + 0.02
        if n == 0:
            # Frame 0 is the thumbnail: the hook is already on screen and settles in.
            js.append(f'tl.fromTo("#{sid}-head", {{scale: 1.12}}, {{scale: 1, duration: 0.45, ease: "power3.out"}}, 0);')
        elif words:
            js.append(f'tl.fromTo("#{sid}-head .w", {{y: {round(head_px * 0.6)}, scale: 1.25, opacity: 0}}, '
                      f'{{y: 0, scale: 1, opacity: 1, duration: 0.32, ease: "back.out(2.2)", stagger: 0.07}}, {a:.2f});')
        if s["kind"] in ("hook", "problem"):
            js.append(f'tl.fromTo("#{sid}-bar", {{scaleX: 0}}, {{scaleX: 1, duration: 0.4, ease: "power2.inOut"}}, {a + 0.25:.2f});')
        if s["image"] >= 0:
            js.append(f'tl.fromTo("#{sid}-pic", {{y: 80, scale: 0.86, opacity: 0}}, {{y: 0, scale: 1, opacity: 1, '
                      f'duration: 0.5, ease: "power3.out"}}, {a + 0.1:.2f});')
            js.append(f'tl.fromTo("#{sid}-pic", {{scale: 1}}, {{scale: 1.04, duration: {max(0.5, dur - 0.7):.2f}, '
                      f'ease: "none"}}, {a + 0.6:.2f});')
        for k in range(len(s["items"])):
            js.append(f'tl.fromTo("#{sid}-i{k}", {{x: -80, opacity: 0}}, {{x: 0, opacity: 1, duration: 0.35, '
                      f'ease: "power3.out"}}, {a + 0.35 + k * 0.4:.2f});')
        if s["sub"] and s["kind"] != "cta":
            js.append(f'tl.fromTo("#{sid}-sub", {{y: 30, opacity: 0}}, {{y: 0, opacity: 1, duration: 0.35}}, {a + 0.45:.2f});')
        if s["kind"] == "cta":
            js.append(f'tl.fromTo("#{sid}-btn", {{scale: 0.5, opacity: 0}}, {{scale: 1, opacity: 1, duration: 0.4, '
                      f'ease: "back.out(2.5)"}}, {a + 0.35:.2f});')
            pulses = max(0, int((dur - 1.2) / 0.9))
            if pulses:
                js.append(f'tl.to("#{sid}-btn", {{scale: 1.07, duration: 0.45, ease: "sine.inOut", yoyo: true, '
                          f'repeat: {pulses * 2 - 1}}}, {a + 0.9:.2f});')
        if not last:
            js.append(f'tl.to("#{sid}-in", {{...{exits[n % len(exits)]}, duration: 0.22, ease: "power2.in"}}, '
                      f'{start + dur - 0.24:.2f});')
    js.append(f'tl.fromTo("#glow", {{x: {-round(base * 0.12)}, y: {-round(base * 0.08)}}}, '
              f'{{x: {round(base * 0.12)}, y: {round(base * 0.08)}, duration: {length}, ease: "sine.inOut"}}, 0);')
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={W}, height={H}" />
    <script src="https://cdn.jsdelivr.net/npm/gsap@3.15.0/dist/gsap.min.js"></script>
    <style>{css}</style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0" data-duration="{length}" data-width="{W}" data-height="{H}">
      <div id="glow" class="clip" data-start="0" data-duration="{length}" data-track-index="0" data-layout-allow-overflow></div>
      {chr(10).join('      ' + b for b in body).strip()}
    </div>
    <script>
      const tl = gsap.timeline({{ paused: true }});
      {chr(10).join('      ' + j for j in js).strip()}
      window.__timelines["main"] = tl;
      tl.seek(0);
    </script>
  </body>
</html>
"""


def write_project(cfg: Config, folder: Path, scenes: list[dict], brief: dict, images: list[Path]) -> Path:
    """A complete HyperFrames project in `folder` (index.html, hyperframes.json, assets/)."""
    folder.mkdir(parents=True, exist_ok=True)
    assets = folder / "assets"
    (assets / "fonts").mkdir(parents=True, exist_ok=True)
    shutil.copy(cfg.resolve("brand/fonts/thumbnail/Anton-Regular.ttf"), assets / "fonts" / "Anton-Regular.ttf")
    shutil.copy(cfg.resolve("brand/fonts/Montserrat-Black.ttf"), assets / "fonts" / "Montserrat-Black.ttf")
    names = []
    for i, img in enumerate(images):
        name = f"img{i}{img.suffix.lower()}"
        shutil.copy(img, assets / name)
        names.append(name)
    size = ASPECTS[brief["aspect"]]
    (folder / "index.html").write_text(build_html(scenes, size, palette(cfg, brief["ground"]), names, brief["length"]),
                                       encoding="utf-8")
    (folder / "hyperframes.json").write_text(json.dumps({
        "$schema": "https://hyperframes.heygen.com/schema/hyperframes.json",
        "paths": {"blocks": "compositions", "components": "compositions/components", "assets": "assets"}},
        indent=2), encoding="utf-8")
    (folder / "storyboard.json").write_text(json.dumps(scenes, indent=1), encoding="utf-8")
    return folder / "index.html"


# --------------------------------------------------------------------------- sound

def sfx_events(scenes: list[dict], level: str) -> list[tuple[str, float]]:
    if level == "off":
        return []
    out = []
    for n, s in enumerate(scenes):
        if n:
            out.append(("whoosh", max(0.0, s["start"] - 0.12)))
        if s["kind"] == "cta":
            out.append(("pop", s["start"] + 0.35))
        if level == "full":
            out += [("tick", s["start"] + 0.35 + k * 0.4) for k in range(len(s["items"]))]
    return out


def add_sound(cfg: Config, silent: Path, out: Path, scenes: list[dict], length: float, music: Path | None, level: str) -> dict:
    """Mix music (if any) and the effects under the picture, then master it like every clip."""
    from . import audio, sfx
    tmp = out.with_suffix(".mix.mkv")
    fx = sfx.track(cfg, sfx_events(scenes, level), length, out.with_suffix(".sfx.wav"))
    inputs, chains, labels = ["-i", silent], [], []
    if music:
        inputs += ["-stream_loop", "-1", "-i", music]
        chains.append(f"[1:a]atrim=0:{length},asetpts=N/SR/TB,volume={cfg.get('animate.music_db', -6)}dB,"
                      f"afade=t=in:d=0.3,afade=t=out:st={max(0, length - 1.2)}:d=1.2,aresample=48000[m]")
        labels.append("[m]")
    if fx:
        inputs += ["-i", fx]
        chains.append(f"[{1 + bool(music)}:a]aresample=48000[f]")
        labels.append("[f]")
    if not labels:
        media.ffmpeg("-i", silent, "-c", "copy", "-movflags", "+faststart", out)
        return {"music": False, "sfx": 0}
    mix = (";".join(chains) + ";" + "".join(labels) + f"amix=inputs={len(labels)}:duration=longest:normalize=0,"
           f"atrim=0:{length}[a]") if len(labels) > 1 else ";".join(chains).replace(labels[0], "[a]")
    media.ffmpeg(*inputs, "-filter_complex", mix, "-map", "0:v:0", "-map", "[a]", "-c:v", "copy",
                 "-c:a", "pcm_s16le", "-t", f"{length}", tmp)
    try:
        audio.finish(tmp, out, cfg)
    finally:
        tmp.unlink(missing_ok=True)
        if fx:
            fx.unlink(missing_ok=True)
    return {"music": bool(music), "sfx": len(sfx_events(scenes, level))}


# --------------------------------------------------------------------------- jobs

def _folder(cfg: Config, ad_id: int) -> Path:
    return cfg.subdir("ads") / str(ad_id)


def ad_dict(row) -> dict:
    d = dict(row)
    for k in ("brief", "scenes", "report"):
        d[k] = json.loads(d[k]) if d.get(k) else None
    d["has_video"] = bool(d.get("video_path") and Path(d["video_path"]).exists())
    return d


def list_ads(conn, limit: int = 40) -> list[dict]:
    return [ad_dict(r) for r in conn.execute("SELECT * FROM ads ORDER BY id DESC LIMIT ?", (limit,))]


def _set(conn, ad_id: int, **fields) -> None:
    fields["updated_at"] = db.iso(db.utcnow())
    for k in ("scenes", "report", "brief"):
        if k in fields and not isinstance(fields[k], str) and fields[k] is not None:
            fields[k] = json.dumps(fields[k])
    conn.execute(f"UPDATE ads SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), ad_id))


def render_ad(cfg: Config, conn, ad_id: int) -> dict:
    """Write the project for one ad and render it. Returns the report."""
    ad = ad_dict(conn.execute("SELECT * FROM ads WHERE id=?", (ad_id,)).fetchone())
    ok, why = ready()
    if not ok:
        raise RuntimeError(why)
    folder = _folder(cfg, ad["batch"])
    project = folder / f"v{ad['variant']}"
    images = sorted((folder / "uploads").glob("img*")) if (folder / "uploads").exists() else []
    music = next(iter(sorted((folder / "uploads").glob("music*"))), None) if (folder / "uploads").exists() else None
    _set(conn, ad_id, status="rendering", error=None, project_path=str(project))
    write_project(cfg, project, ad["scenes"], ad["brief"], images)
    silent = project / "renders" / "silent.mp4"
    silent.parent.mkdir(exist_ok=True)
    with _lock:                                    # one render at a time: each one uses every CPU core
        hyperframes(["lint"], project, timeout=300)
        hyperframes(["render", "--output", str(silent), "--fps", str(cfg.get("animate.fps", 30)),
                     "--quality", "delivery"], project)
    out = folder / f"ad_{ad_id}_v{ad['variant']}.mp4"
    report = add_sound(cfg, silent, out, ad["scenes"], float(ad["brief"]["length"]), music, ad["brief"]["sfx"])
    silent.unlink(missing_ok=True)
    report = {**(ad["report"] or {}), **report, "seconds": ad["brief"]["length"]}
    _set(conn, ad_id, status="ready", video_path=str(out), report=report)
    db.log_event(conn, "ad_rendered", ad_id, {"variant": ad["variant"], "seconds": ad["brief"]["length"]})
    return report


def plan(cfg: Config, conn, batch_id: int, llm) -> list[int]:
    """Storyboard every variation of a batch and add one `ads` row each."""
    row = conn.execute("SELECT * FROM ads WHERE id=?", (batch_id,)).fetchone()
    brief = json.loads(row["brief"])
    uploads = _folder(cfg, batch_id) / "uploads"
    images = sorted(p.name for p in uploads.glob("img*")) if uploads.exists() else []
    labels = json.loads(row["report"] or "{}").get("image_labels") or images
    raw = llm(*_prompt(brief, labels), STORYBOARD_SCHEMA)
    variations = normalise(raw, brief, len(images))
    missing = [m for m in raw.get("missing", []) if m.strip()][:8]
    ids = []
    for n, v in enumerate(variations, 1):
        if n == 1:
            _set(conn, batch_id, angle=v["angle"], scenes=v["scenes"], status="queued",
                 report={"missing": missing, "image_labels": labels})
            ids.append(batch_id)
        else:
            now = db.iso(db.utcnow())
            ids.append(conn.execute(
                """INSERT INTO ads(batch, variant, name, brief, angle, scenes, status, report, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (batch_id, n, row["name"], row["brief"], v["angle"], json.dumps(v["scenes"]), "queued",
                 json.dumps({"missing": missing}), now, now)).lastrowid)
    return ids


def create(cfg: Config, conn, raw_brief: dict, images: list[tuple[str, Path]], music: Path | None,
           llm=None, background: bool = True) -> int:
    """Start a batch: save the uploads, storyboard it with Claude, render every variation in the background."""
    brief = clean_brief(raw_brief)
    now = db.iso(db.utcnow())
    batch = conn.execute(
        """INSERT INTO ads(batch, variant, name, brief, status, created_at, updated_at) VALUES (0,1,?,?,?,?,?)""",
        (brief["product"][:80], json.dumps(brief), "planning", now, now)).lastrowid
    conn.execute("UPDATE ads SET batch=? WHERE id=?", (batch, batch))
    uploads = _folder(cfg, batch) / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    labels = []
    for i, (label, path) in enumerate(images[:MAX_IMAGES]):
        shutil.move(str(path), uploads / f"img{i}{path.suffix.lower() or '.png'}")
        labels.append(label or f"image {i}")
    if music:
        shutil.move(str(music), uploads / f"music{music.suffix.lower() or '.mp3'}")
    _set(conn, batch, report={"image_labels": labels})

    def job():
        c = db.connect(cfg.db_path)
        try:
            ids = plan(cfg, c, batch, llm)
        except Exception as exc:
            log.exception("Ad %s storyboard failed", batch)
            _set(c, batch, status="failed", error=str(exc)[:500])
            return
        for ad_id in ids:
            try:
                render_ad(cfg, c, ad_id)
            except Exception as exc:
                log.exception("Ad %s render failed", ad_id)
                _set(c, ad_id, status="failed", error=str(exc)[-500:])

    if background:
        threading.Thread(target=job, name=f"ad-{batch}", daemon=True).start()
    else:
        job()
    return batch


def rerender(cfg: Config, conn, ad_id: int, scenes: list[dict] | None = None, background: bool = True) -> None:
    """Render again, optionally with edited scene text (same timings and designs)."""
    row = conn.execute("SELECT * FROM ads WHERE id=?", (ad_id,)).fetchone()
    if row is None:
        raise ValueError("That ad is gone")
    if scenes is not None:
        old = json.loads(row["scenes"] or "[]")
        if len(scenes) != len(old):
            raise ValueError("Edit the words of the scenes; adding or removing scenes isn't supported here")
        for s, new in zip(old, scenes):
            s["headline"] = _words(new.get("headline", s["headline"]), MAX_WORDS) or s["headline"]
            s["sub"] = _words(new.get("sub", s["sub"]), 14)
            if s["emphasis"] and s["emphasis"].lower() not in s["headline"].lower():
                s["emphasis"] = ""
            if s["items"]:
                s["items"] = [_words(i, 6) for i in (new.get("items") or s["items"])][:3]
        _set(conn, ad_id, scenes=old)
    _set(conn, ad_id, status="queued", error=None)

    def job():
        c = db.connect(cfg.db_path)
        try:
            render_ad(cfg, c, ad_id)
        except Exception as exc:
            log.exception("Ad %s render failed", ad_id)
            _set(c, ad_id, status="failed", error=str(exc)[-500:])

    if background:
        threading.Thread(target=job, name=f"ad-r{ad_id}", daemon=True).start()
    else:
        job()


_previews: dict[int, subprocess.Popen] = {}


def open_editor(cfg: Config, conn, ad_id: int) -> str:
    """Open the ad's project in HyperFrames' own editor (a local page in the browser)."""
    row = conn.execute("SELECT project_path FROM ads WHERE id=?", (ad_id,)).fetchone()
    if not row or not row["project_path"] or not Path(row["project_path"]).exists():
        raise ValueError("Render the ad first")
    ok, why = ready()
    if not ok:
        raise ValueError(why)
    old = _previews.get(ad_id)
    if old and old.poll() is None:
        return "already open"
    _previews[ad_id] = subprocess.Popen([shutil.which("npx") or "npx", "--yes", HYPERFRAMES, "preview"],
                                        cwd=row["project_path"], env=_env(), stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, creationflags=media._CREATE_NO_WINDOW)
    return "opening"


def resume(cfg: Config, conn) -> None:
    """After a restart: anything that was mid-render goes back to failed with a retry button."""
    conn.execute("UPDATE ads SET status='failed', error='The app was closed during this render. Press Render again.' "
                 "WHERE status IN ('planning','queued','rendering')")
