"""Stop-motion looks, from the master-skills /edit skill (Jake Schincariol, MIT), done with ffmpeg filters.

Two ways in:
  - live video (a clip or an uploaded take): the picture drops to 8, 10 or 12 drawings a second and
    every drawing sits a few pixels off and a fraction of a degree askew, like a hand-placed frame.
    Captions, the hook and the logo are drawn afterwards, so they stay smooth and readable.
  - a folder of photos: each photo becomes one drawing, held for `hold` frames, with the same wobble.

Looks (all deterministic: the same seed gives the same wobble):
  classic  wobble only
  clay     softened, slightly saturated, a little exposure flicker
  poster   flat colour bands, like paper cut-outs
  pixel    chunky pixels
"""

from __future__ import annotations

import math
import os
import random
import re
import shutil
import tempfile
from pathlib import Path

from . import media

LOOKS = ("classic", "clay", "poster", "pixel")
DRAWING_RATES = (8, 10, 12)
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")


def hash_expr(var: str, a: float, b: float) -> str:
    """Deterministic pseudo-random 0..1 per drawing: fract(sin(i*a + b) * 43758.5453)."""
    inner = f"sin({var}*{a:.4f}+{b:.4f})*43758.5453"
    return f"({inner}-floor({inner}))"


def jitter_chain(width: int, height: int, jitter: float, rotate_deg: float, flicker: float, seed: int,
                 index_var: str = "n") -> str:
    """rotate + crop + scale; the crop margin hides the rotated corners."""
    rng = random.Random(seed)
    offsets = [rng.uniform(0.0, 100.0) for _ in range(4)]
    h_rot = hash_expr(index_var, 12.9898, offsets[0])
    h_x = hash_expr(index_var, 78.2330, offsets[1])
    h_y = hash_expr(index_var, 39.3468, offsets[2])
    margin = int(math.ceil(jitter + max(width, height) / 2 * math.sin(math.radians(rotate_deg)) + 2))
    margin += margin % 2
    parts = []
    if rotate_deg > 0:
        parts.append(f"rotate=a='{rotate_deg * math.pi / 180:.6f}*(2*{h_rot}-1)':c=black:ow=iw:oh=ih")
    if jitter > 0 or rotate_deg > 0:
        parts.append(f"crop=w=iw-{2 * margin}:h=ih-{2 * margin}:"
                     f"x='{margin}+{jitter:.3f}*(2*{h_x}-1)':y='{margin}+{jitter:.3f}*(2*{h_y}-1)'")
        parts.append(f"scale={width}:{height}:flags=bicubic")
    if flicker > 0:
        h_b = hash_expr(index_var, 93.9898, offsets[3])
        parts.append(f"eq=brightness='{flicker:.4f}*(2*{h_b}-1)':eval=frame")
    parts.append("setsar=1")
    return ",".join(parts)


def look_chain(look: str, width: int, height: int) -> str:
    """Colour / texture treatment applied once per drawing, before the wobble."""
    if look == "clay":
        return "hqdn3d=4:3:6:4.5,eq=saturation=1.25:contrast=1.06"
    if look == "poster":
        return "eq=saturation=1.15,lutrgb=r='floor(val/48)*48+24':g='floor(val/48)*48+24':b='floor(val/48)*48+24'"
    if look == "pixel":
        return (f"scale={max(16, width // 14) // 2 * 2}:{max(16, height // 14) // 2 * 2}:flags=neighbor,"
                f"scale={width}:{height}:flags=neighbor")
    return ""


def video_filter(width: int, height: int, out_fps: int, settings: dict, index_var: str = "n") -> str:
    """Filter chain that turns smooth video into stop motion at the same size and output frame rate."""
    look = settings.get("look", "classic")
    drawings = int(settings.get("fps", 12))
    jitter = float(settings.get("jitter", min(width, height) * 0.005))
    if look == "pixel":
        jitter = 0.0
    rotate = float(settings.get("rotate", 0.5))
    flicker = float(settings.get("flicker", 0.012 if look == "clay" else 0.0))
    seed = int(settings.get("seed", 1))
    parts = [f"fps={drawings}"]
    treat = look_chain(look, width, height)
    if treat:
        parts.append(treat)
    parts.append(jitter_chain(width, height, jitter, rotate, flicker, seed, index_var))
    parts.append(f"fps={out_fps}")
    return ",".join(parts)


def normalize_settings(raw: dict | None) -> dict | None:
    if not raw or not raw.get("look"):
        return None
    look = raw["look"] if raw["look"] in LOOKS else "classic"
    fps = int(raw.get("fps", 12))
    return {"look": look, "fps": fps if fps in DRAWING_RATES else 12, "seed": int(raw.get("seed", 1))}


def natural_key(name: str):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", name)]


def from_photos(photos: list[Path], out: Path, *, fps: int = 12, hold: int = 2, width: int = 1080, height: int = 1920,
                fit: str = "cover", bg: str = "#F4EFE6", look: str = "classic", seed: int = 1, loop: int = 1,
                pingpong: bool = False, end_hold: float = 0.0, audio: Path | None = None, cfg=None) -> Path:
    """Photos in natural filename order -> stop-motion video (each photo is one drawing)."""
    photos = sorted((Path(p) for p in photos if Path(p).suffix.lower() in IMAGE_EXTS), key=lambda p: natural_key(p.name))
    if not photos:
        raise ValueError("Add at least one photo (png, jpg, webp, bmp or tif)")
    if not 1 <= hold <= 8 or fps * hold > 60:
        raise ValueError("Hold each photo 1-8 frames, and keep drawings x hold at 60 fps or less")
    order = list(range(len(photos)))
    if pingpong and len(photos) > 2:
        order += list(range(len(photos) - 2, 0, -1))
    order *= max(1, loop)
    out_fps = fps * hold
    scale = "increase" if fit == "cover" else "decrease"
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kir_stop_") as tmp:
        norm_dir, seq_dir = Path(tmp) / "norm", Path(tmp) / "seq"
        norm_dir.mkdir()
        seq_dir.mkdir()
        normed = []
        for i, src in enumerate(photos):
            dst = norm_dir / f"{i:05d}.png"
            graph = (f"color=c={bg}:s={width}x{height}:d=1[bg];"
                     f"[0:v]scale={width}:{height}:force_original_aspect_ratio={scale},format=rgba[fg];"
                     f"[bg][fg]overlay=(W-w)/2:(H-h)/2:format=auto,format=rgb24")
            media.ffmpeg("-i", src, "-filter_complex", graph, "-frames:v", "1", dst)
            normed.append(dst)
        n = 0
        for idx in order:  # each drawing repeated `hold` times: exact frame counts
            for _ in range(hold):
                link = seq_dir / f"{n:06d}.png"
                try:
                    os.symlink(normed[idx], link)
                except OSError:
                    shutil.copy(normed[idx], link)
                n += 1
        treat = look_chain(look, width, height)
        jitter = 0.0 if look == "pixel" else min(width, height) * 0.005
        vf = ((treat + ",") if treat else "") + jitter_chain(width, height, jitter, 0.5,
                                                              0.012 if look == "clay" else 0.0, seed,
                                                              f"floor(n/{hold})")
        if end_hold > 0:
            vf += f",tpad=stop_mode=clone:stop_duration={end_hold:g}"
        vf += ",format=yuv420p"
        args = ["-framerate", str(out_fps), "-i", seq_dir / "%06d.png"]
        if audio:
            args += ["-i", audio, "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "192k", "-shortest"]
        r = (cfg.get("render") if cfg else None) or {}
        args += ["-vf", vf, "-r", str(out_fps), *media.encoder_args("libx264", int(r.get("crf", 20))),
                 "-movflags", "+faststart", out]
        media.ffmpeg(*args)
    return out
