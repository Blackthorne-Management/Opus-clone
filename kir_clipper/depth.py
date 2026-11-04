"""The hook word behind your head: one big word sits behind you while the captions stay in front.

How: a person cut-out model (rembg; u2net_human_seg by default, about 0.4 s a frame on a normal CPU, or
birefnet-portrait for finer hair if the PC has the memory) makes a soft mask of you, only for the few
seconds the word is on screen. The word is drawn in Anton capitals about 85% of the frame wide, placed so
the top of your head reaches a little over halfway up the letters, then everything that is you is put back
on top of it. It pops in (80% -> 105% -> 100% over 6 frames) on the frame you say it, holds, and fades.

Needs space above your head when you film. When there isn't (no head found, or the head is at the very top),
the clip renders normally and the card says why.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from . import media
from .captions import is_emphasis

log = logging.getLogger("kir.depth")

STOP = set("""THE AND THAT THIS WITH YOUR YOU'RE THEY THEM THEIR THERE WHAT WHEN WHERE WHICH WOULD COULD SHOULD
ABOUT JUST LIKE REALLY HAVE BEEN FROM INTO BECAUSE GONNA WANNA KNOW THING THINGS PEOPLE""".split())
_sessions: dict[str, object] = {}


def available() -> bool:
    try:
        import rembg  # noqa: F401
        return True
    except ImportError:
        return False


def pick_word(words: list[dict], wanted: str = "", within: float = 4.0) -> dict | None:
    """The word to put behind your head: the one you asked for, else the strongest in the first few seconds
    (money, numbers and power words first, then the longest real word)."""
    early = [w for w in words if w["s"] <= within]
    clean = lambda w: re.sub(r"[^\w$%']", "", w["w"]).upper()
    if wanted.strip():
        target = re.sub(r"[^\w$%']", "", wanted).upper()
        for w in words:
            if clean(w) == target:
                return {"text": target, "s": w["s"]}
        return None
    strong = [w for w in early if is_emphasis(w["w"])]
    if strong:
        return {"text": clean(strong[0]), "s": strong[0]["s"]}
    long = sorted((w for w in early if len(clean(w)) >= 4 and clean(w) not in STOP), key=lambda w: -len(clean(w)))
    return {"text": clean(long[0]), "s": long[0]["s"]} if long else None


def _session(model: str, cache_dir: Path):
    if model not in _sessions:
        os.environ.setdefault("U2NET_HOME", str(cache_dir))     # model downloads stay in the app's data folder
        from rembg import new_session
        _sessions[model] = new_session(model)
    return _sessions[model]


def person_mask(frame: np.ndarray, session, work_width: int = 512) -> np.ndarray:
    """Soft mask (0..1 float32, frame size) of the person in an RGB frame."""
    from PIL import Image
    from rembg import remove
    h, w = frame.shape[:2]
    small = Image.fromarray(frame).resize((work_width, round(h * work_width / w)), Image.BILINEAR)
    m = remove(small, session=session, only_mask=True)
    return np.asarray(m.resize((w, h), Image.BILINEAR), dtype=np.float32) / 255.0


def head_top(mask: np.ndarray) -> int | None:
    """Top row of the head, looking only at the middle 40% so a raised hand doesn't count."""
    h, w = mask.shape
    mid = mask[:, int(w * 0.3): int(w * 0.7)]
    rows = np.where((mid > 0.5).sum(axis=1) > max(3, mid.shape[1] * 0.02))[0]
    return int(rows[0]) if rows.size else None


def word_layer(text: str, font_path: Path, width: int, height: int, colour=(255, 255, 255)) -> np.ndarray:
    """The word as an RGBA image `width` wide (tightly cropped)."""
    from PIL import Image, ImageDraw, ImageFont
    size = 200
    font = ImageFont.truetype(str(font_path), size)
    l, t, r, b = font.getbbox(text)
    size = max(20, int(size * width / max(1, r - l)))
    font = ImageFont.truetype(str(font_path), size)
    l, t, r, b = font.getbbox(text)
    img = Image.new("RGBA", (r - l + 4, b - t + 4), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((2 - l, 2 - t), text, font=font, fill=(*colour, 255))
    if img.height > height * 0.45:                          # a very short word: don't fill half the screen
        img = img.resize((int(img.width * height * 0.45 / img.height), int(height * 0.45)), Image.LANCZOS)
    return np.asarray(img, dtype=np.uint8)


def place(word: np.ndarray, mask: np.ndarray, top_of_head: int, min_cover: float = 0.15) -> tuple[int, int] | None:
    """(x, y) for the word's top-left: head covers ~60% of its height, and at least `min_cover` of its letters."""
    H, W = mask.shape
    wh, ww = word.shape[:2]
    x = (W - ww) // 2
    y = int(top_of_head - 0.4 * wh)
    alpha = word[..., 3].astype(np.float32) / 255
    for _ in range(12):
        if y < int(H * 0.03):
            y = int(H * 0.03)
        region = mask[y: y + wh, x: x + ww]
        if region.shape != alpha.shape:
            return None
        cover = float((alpha * region).sum() / max(1.0, alpha.sum()))
        if cover >= min_cover:
            return x, y
        y += int(H * 0.02)                                     # lower it until the head covers enough
    return None


def _scale(word: np.ndarray, factor: float) -> np.ndarray:
    from PIL import Image
    if abs(factor - 1) < 1e-3:
        return word
    img = Image.fromarray(word)
    return np.asarray(img.resize((max(1, int(img.width * factor)), max(1, int(img.height * factor))), Image.LANCZOS))


POP = (0.8, 0.9, 0.98, 1.05, 1.02, 1.0)                        # 6 frames: pop in with a small overshoot


def render(cfg, video: Path, out: Path, word: dict, fps: int, duration: float, mask_fn=None,
           font_path: Path | None = None, colour=(255, 255, 255)) -> dict:
    """Overlay the word behind the person in `video` (must have a video stream), writing `out` (audio copied).
    Returns a report {"word", "start", "placed", "reason"}. `mask_fn(frame)` can stand in for the model (tests)."""
    info = media.probe(video)
    W, H = info["width"], info["height"]
    start = max(0.0, float(word["s"]))
    hold = float(cfg.get("depth.hold_seconds", 3.0))
    end = min(duration, start + hold)
    n = max(1, int(round((end - start) * fps)))
    report = {"word": word["text"], "start": round(start, 2), "placed": False, "reason": ""}
    if end - start < 0.5:
        report["reason"] = "the word comes too late in the clip"
        return report
    if mask_fn is None:
        if not available():
            report["reason"] = "the cut-out model isn't installed (run Install.bat again)"
            return report
        session = _session(cfg.get("depth.model", "u2net_human_seg"), cfg.subdir("models"))
        mask_fn = lambda f: person_mask(f, session)
    font_path = font_path or cfg.resolve("brand/fonts/thumbnail/Anton-Regular.ttf")
    reader = subprocess.Popen([media._bin("ffmpeg"), "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}",
                               "-i", str(video), "-frames:v", str(n), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                              stdout=subprocess.PIPE, creationflags=media._CREATE_NO_WINDOW)
    frame_bytes = W * H * 3
    first = reader.stdout.read(frame_bytes)
    if len(first) < frame_bytes:
        reader.kill()
        report["reason"] = "couldn't read the video"
        return report
    frame0 = np.frombuffer(first, np.uint8).reshape(H, W, 3)
    mask0 = mask_fn(frame0)
    top = head_top(mask0)
    if top is None or top < H * 0.08:
        reader.kill()
        report["reason"] = "no space above your head in this shot" if top is not None else "no person found in the shot"
        return report
    base = word_layer(word["text"], font_path, int(W * float(cfg.get("depth.word_width", 0.85))), H, colour)
    spot = place(base, mask0, top)
    if spot is None:
        reader.kill()
        report["reason"] = "your head doesn't reach the word in this shot"
        return report
    bx, by = spot
    cx, cy = bx + base.shape[1] / 2, by + base.shape[0] / 2

    with tempfile.TemporaryDirectory(prefix="kir_depth_") as tmp:
        layer_path = Path(tmp) / "layer.mov"
        writer = subprocess.Popen([media._bin("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
                                   "-pix_fmt", "rgba", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
                                   "-c:v", "qtrle", str(layer_path)], stdin=subprocess.PIPE,
                                  creationflags=media._CREATE_NO_WINDOW)
        fade = max(1, int(fps * 0.2))
        try:
            for k in range(n):
                if k == 0:
                    frame, mask = frame0, mask0
                else:
                    raw = reader.stdout.read(frame_bytes)
                    if len(raw) < frame_bytes:
                        break
                    frame = np.frombuffer(raw, np.uint8).reshape(H, W, 3)
                    mask = mask_fn(frame)
                w = _scale(base, POP[k] if k < len(POP) else 1.0)
                layer = np.zeros((H, W, 4), np.float32)
                x0, y0 = int(cx - w.shape[1] / 2), int(cy - w.shape[0] / 2)
                xs, ys = max(0, x0), max(0, y0)
                xe, ye = min(W, x0 + w.shape[1]), min(H, y0 + w.shape[0])
                layer[ys:ye, xs:xe] = w[ys - y0: ye - y0, xs - x0: xe - x0]
                fade_k = min(1.0, (n - k) / fade)                 # fade out over the last 0.2 s
                layer[..., 3] *= (1 - mask) * fade_k                # you stay in front of the word
                writer.stdin.write(layer.clip(0, 255).astype(np.uint8).tobytes())
        finally:
            writer.stdin.close()
            writer.wait()
            reader.kill()
        r = cfg.get("render") or {}
        enc = media.pick_encoder(r.get("encoder", "auto"))
        args = ["-i", video, "-itsoffset", f"{start:.3f}", "-i", layer_path,
                "-filter_complex", "[0:v][1:v]overlay=0:0:eof_action=pass:format=auto[v]",
                "-map", "[v]", "-map", "0:a?", "-c:a", "copy"]
        try:
            media.ffmpeg(*args, *media.encoder_args(enc, int(r.get("crf", 20)), int(r.get("nvenc_cq", 23))), out)
        except RuntimeError:
            if enc == "libx264":
                raise
            media.ffmpeg(*args, *media.encoder_args("libx264", int(r.get("crf", 20))), out)
    report["placed"] = True
    return report
