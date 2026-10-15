"""Render a clip to a 1080x1920 master from a recipe.

A recipe is a small JSON dict that fully describes the edit (caption style, crop mode, zoom,
hook text, overlays). It is stored with every clip so the 90-day job can render a version that
is meaningfully different from every earlier one.
"""

from __future__ import annotations

import json
import logging
import shutil
import statistics
import tempfile
from pathlib import Path

from . import media
from .captions import build_ass
from .config import Config

log = logging.getLogger("kir.render")

CROP_MODES = ("face", "center", "blur_pad")
ZOOM_MODES = ("none", "punch", "slow_push")


def default_recipe(cfg: Config, clip: dict) -> dict:
    d = dict(cfg.get("render.default_recipe") or {})
    return {
        "version": 1,
        "caption_style": d.get("caption_style", "hormozi"),
        "caption_position": d.get("caption_position", "lower"),
        "crop_mode": d.get("crop_mode", "face"),
        "zoom": d.get("zoom", "punch"),
        "zoom_period": float(d.get("zoom_period", 6.0)),
        "zoom_amount": float(d.get("zoom_amount", 0.10)),
        "hook_overlay": bool(d.get("hook_overlay", True)),
        "hook_text": clip.get("hook_overlay") or clip.get("hook") or "",
        "hook_text_alt": clip.get("hook_overlay_alt") or "",
        "outro_cta": bool(d.get("outro_cta", True)),
        "cta_text": cfg.get("brand.cta_text", ""),
        "watermark": True,
        "watermark_corner": "tr",
        "trim_start": 0.0,
        "trim_end": 0.0,
        "aspect": "9:16",
    }


def filter_path(p: Path) -> str:
    """Path quoted for use inside an ffmpeg filter argument (handles Windows drive colons)."""
    s = Path(p).resolve().as_posix().replace("'", r"\'").replace(":", r"\:")
    return f"'{s}'"


_face_warned = False


def detect_face_center(source: Path, start: float, end: float, samples: int = 12) -> float | None:
    """Median horizontal face position (0..1) across the clip, or None if no reliable face.

    Face tracking is a nice-to-have: any failure here means a center crop, never a failed render.
    """
    global _face_warned
    try:
        return _detect_face_center(source, start, end, samples)
    except Exception as exc:
        if not _face_warned:
            _face_warned = True
            log.warning("Face tracking unavailable (%s: %s); using center crop. "
                        "Fix: .venv\\Scripts\\python -m pip install \"opencv-python-headless<5\"",
                        type(exc).__name__, exc)
        return None


def _detect_face_center(source: Path, start: float, end: float, samples: int) -> float | None:
    try:
        import cv2  # optional dependency
    except ImportError:
        return None
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap = cv2.VideoCapture(str(source))
    xs = []
    try:
        for k in range(samples):
            t = start + (end - start) * (k + 0.5) / samples
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            h, w = frame.shape[:2]
            scale = 640 / w
            small = cv2.resize(frame, (640, int(h * scale)))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(40, 40))
            if len(faces):
                x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
                xs.append((x + fw / 2) / 640)
    finally:
        cap.release()
    if len(xs) < max(3, samples // 3):
        return None
    return float(statistics.median(xs))


def build_filtergraph(info: dict, recipe: dict, out_w: int, out_h: int, fps: int, face_x: float | None,
                      has_logo: bool, ass_name: str, fonts_dir: Path | None) -> str:
    src_w, src_h = info["width"], info["height"]
    mode = recipe.get("crop_mode", "center")
    chains = []
    if mode == "blur_pad":
        chains.append(
            f"[0:v]split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h},boxblur=24:2,"
            f"eq=brightness=-0.08[bg];"
            f"[fgsrc]scale={out_w}:-2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[base]"
        )
    else:
        target_ratio = out_w / out_h
        if src_w / max(src_h, 1) > target_ratio:
            crop_w, crop_h = int(src_h * target_ratio) // 2 * 2, src_h
        else:
            crop_w, crop_h = src_w, int(src_w / target_ratio) // 2 * 2
        cx = (face_x if (mode == "face" and face_x is not None) else 0.5) * src_w
        x = int(min(max(cx - crop_w / 2, 0), src_w - crop_w))
        y = int((src_h - crop_h) / 2)
        chains.append(f"[0:v]crop={crop_w}:{crop_h}:{x}:{y},scale={out_w}:{out_h}:flags=lanczos,setsar=1[base]")

    zoom = recipe.get("zoom", "none")
    amount = float(recipe.get("zoom_amount", 0.1))
    period = float(recipe.get("zoom_period", 6.0))
    if zoom == "punch":
        z = f"1+{amount}*between(mod(in/{fps},{period}),{period / 2},{period})"
    elif zoom == "slow_push":
        z = f"min(1+{amount}*in/{fps}/30,{1 + amount * 1.5})"
    else:
        z = None
    last = "base"
    # Mirroring flips the picture only; captions, hook text and logo are added afterwards so they stay readable.
    pre = f"[base]{'hflip,' if recipe.get('mirror') else ''}fps={fps}"
    if z:
        chains.append(f"{pre},zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={out_w}x{out_h}:fps={fps}[zoomed]")
    else:
        chains.append(f"{pre}[zoomed]")
    last = "zoomed"

    if has_logo:
        corner = recipe.get("watermark_corner", "tr")
        pos = {"tr": "W-w-48:64", "tl": "48:64", "br": "W-w-48:H-h-64", "bl": "48:H-h-64"}.get(corner, "W-w-48:64")
        chains.append(f"[1:v]scale=170:-1,format=rgba,colorchannelmixer=aa={recipe.get('logo_opacity', 0.85)}[logo];"
                      f"[{last}][logo]overlay={pos}[branded]")
        last = "branded"

    subs = f"subtitles={ass_name}"
    if fonts_dir and fonts_dir.is_dir() and any(p.suffix.lower() in (".ttf", ".otf") for p in fonts_dir.iterdir()):
        subs += f":fontsdir={filter_path(fonts_dir)}"
    chains.append(f"[{last}]{subs}[vout]")
    return ";".join(chains)


def render_clip(cfg: Config, source: Path, clip: dict, words: list[dict], recipe: dict, out_path: Path,
                info: dict | None = None) -> Path:
    """Cut, reframe, zoom, brand, and caption one clip. `words` are relative to the clip start."""
    info = info or media.probe(source)
    r = cfg.get("render")
    out_w, out_h = int(r.get("width", 1080)), int(r.get("height", 1920))
    fps = int(r.get("fps", 30))
    start = float(clip["start"]) + float(recipe.get("trim_start", 0.0))
    end = float(clip["end"]) - float(recipe.get("trim_end", 0.0))
    duration = max(1.0, end - start)
    shift = float(recipe.get("trim_start", 0.0))
    words = [{**w, "s": w["s"] - shift, "e": w["e"] - shift} for w in words if w["s"] - shift >= -0.05
             and w["e"] - shift <= duration + 0.3]

    face_x = None
    if recipe.get("crop_mode") == "face":
        face_x = detect_face_center(source, start, end)
        if face_x is None:
            log.info("No reliable face found; using center crop")
    brand = cfg.get("brand") or {}
    logo = cfg.resolve(brand["logo"]) if brand.get("logo") else None
    has_logo = bool(recipe.get("watermark") and logo and logo.exists())
    fonts_dir = cfg.resolve(brand["fonts_dir"]) if brand.get("fonts_dir") else None

    out_path.parent.mkdir(parents=True, exist_ok=True)
    encoder = media.pick_encoder(r.get("encoder", "auto"))
    with tempfile.TemporaryDirectory(prefix="kir_render_") as tmp:
        tmpdir = Path(tmp)
        # ffmpeg runs with cwd=tmpdir so the subtitle file can be referenced by bare name.
        (tmpdir / "captions.ass").write_text(build_ass(words, duration, recipe, brand, out_w, out_h), encoding="utf-8")
        graph = build_filtergraph(info, recipe, out_w, out_h, fps, face_x, has_logo, "captions.ass", fonts_dir)
        args = ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source)]
        if has_logo:
            args += ["-i", str(logo)]
        args += ["-filter_complex", graph, "-map", "[vout]"]
        if info.get("has_audio", True):
            args += ["-map", "0:a:0", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac",
                     "-b:a", r.get("audio_bitrate", "192k"), "-ar", "48000"]
        args += media.encoder_args(encoder, int(r.get("crf", 20)), int(r.get("nvenc_cq", 23)))
        tmp_out = tmpdir / "out.mp4"
        args += ["-movflags", "+faststart", str(tmp_out)]
        try:
            media.ffmpeg(*args, cwd=tmpdir)
        except RuntimeError:
            if encoder != "libx264":
                log.warning("Hardware encode failed; retrying on CPU")
                idx = args.index("-c:v")
                args[idx: idx + len(media.encoder_args(encoder))] = media.encoder_args("libx264", int(r.get("crf", 20)))
                media.ffmpeg(*args, cwd=tmpdir)
            else:
                raise
        shutil.move(str(tmp_out), out_path)
    out_path.with_suffix(".recipe.json").write_text(json.dumps(recipe, indent=2), encoding="utf-8")
    log.info("Rendered %s (%.1fs, %s, %s captions, %s zoom)", out_path.name, duration, recipe.get("crop_mode"),
             recipe.get("caption_style"), recipe.get("zoom"))
    return out_path
