"""ffmpeg / ffprobe helpers. All paths go through pathlib so Windows paths work unchanged."""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("kir.media")

# Hide console windows when running as a background service on Windows.
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _bin(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"{name} not found on PATH. Install ffmpeg (e.g. `winget install Gyan.FFmpeg`).")
    return found


def run(args: list[str], cwd: Path | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
    log.debug("run: %s", " ".join(map(str, args)))
    proc = subprocess.run(
        [str(a) for a in args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, creationflags=_CREATE_NO_WINDOW,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-2000:]
        raise RuntimeError(f"{Path(args[0]).name} failed ({proc.returncode}): {tail}")
    return proc


def ffmpeg(*args, cwd: Path | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
    return run([_bin("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *args], cwd=cwd, timeout=timeout)


def probe(path: Path) -> dict:
    out = run([_bin("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    data = json.loads(out.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    fps = 30.0
    if video and video.get("avg_frame_rate", "0/0") != "0/0":
        num, den = video["avg_frame_rate"].split("/")
        fps = float(num) / float(den) if float(den) else 30.0
    return {
        "duration": float(data.get("format", {}).get("duration") or 0.0),
        "width": int(video["width"]) if video else 0,
        "height": int(video["height"]) if video else 0,
        "fps": fps,
        "has_audio": audio is not None,
        "format": data.get("format", {}).get("format_name", ""),
    }


def fast_hash(path: Path, sample_bytes: int = 8 * 1024 * 1024) -> str:
    """Content fingerprint: size + head/middle/tail samples. Fast on multi-GB recordings."""
    size = path.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with open(path, "rb") as fh:
        for offset in (0, max(0, size // 2 - sample_bytes // 2), max(0, size - sample_bytes)):
            fh.seek(offset)
            h.update(fh.read(sample_bytes))
    return h.hexdigest()


def remux_to_mp4(src: Path, dst: Path) -> Path:
    ffmpeg("-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart", dst)
    return dst


def extract_audio(src: Path, dst: Path, start: float = 0.0, duration: float | None = None) -> Path:
    """16 kHz mono WAV, optionally a slice (used for chunked transcription)."""
    args = ["-ss", f"{start:.3f}"]
    if duration is not None:
        args += ["-t", f"{duration:.3f}"]
    ffmpeg(*args, "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", dst)
    return dst


@functools.lru_cache(maxsize=4)
def pick_encoder(preference: str = "auto") -> str:
    """Use NVENC when the GPU can actually encode, otherwise libx264."""
    if preference not in ("auto", ""):
        return preference
    try:
        ffmpeg("-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-",
               timeout=30)
        log.info("Using NVIDIA NVENC hardware encoder")
        return "h264_nvenc"
    except Exception:
        log.info("NVENC unavailable, using libx264 (CPU)")
        return "libx264"


def encoder_args(encoder: str, crf: int = 20, nvenc_cq: int = 23) -> list[str]:
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(nvenc_cq), "-b:v", "0",
                "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-pix_fmt", "yuv420p"]
