"""Sound effects under the voice: a pop when the hook lands, a whoosh on each punch-in zoom, a tick on
money, numbers and power words. Quiet, and mixed before mastering, so the voice stays on top.

The three default sounds are generated here (no sound library, nothing to license). Put your own as
brand/sfx/pop.wav, whoosh.wav or tick.wav (mp3 and m4a work too) and they're used instead.

Levels: off | light (the hook only) | medium (+ zooms) | full (+ emphasis words, at most one every 1.5 s).
"""

from __future__ import annotations

import logging
import wave
from pathlib import Path

import numpy as np

from . import media
from .captions import is_emphasis

log = logging.getLogger("kir.sfx")

SR = 48000
KINDS = ("pop", "whoosh", "tick")
AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg")


def _envelope(n: int, attack: int, decay_rate: float) -> np.ndarray:
    t = np.arange(n, dtype=np.float32)
    env = np.exp(-t / max(1.0, decay_rate))
    env[:attack] *= np.linspace(0, 1, attack, dtype=np.float32)
    return env


def synth(kind: str) -> np.ndarray:
    """A short mono float32 sound at 48 kHz, peak 1.0."""
    rng = np.random.default_rng(7)
    if kind == "pop":
        n = int(0.09 * SR)
        freq = np.linspace(950, 380, n)                       # a quick downward blip
        y = np.sin(2 * np.pi * np.cumsum(freq) / SR) * _envelope(n, 40, 0.025 * SR)
    elif kind == "tick":
        n = int(0.03 * SR)
        y = (rng.standard_normal(n) * 0.5 + np.sin(2 * np.pi * 2600 * np.arange(n) / SR)) * _envelope(n, 8, 0.004 * SR)
    elif kind == "whoosh":
        n = int(0.42 * SR)
        noise = rng.standard_normal(n)
        # Band-limited noise whose brightness rises then falls: two moving averages of changing width.
        widths = (40 - 34 * np.sin(np.linspace(0, np.pi, n))).astype(int)
        csum = np.concatenate([[0.0], np.cumsum(noise)])
        idx = np.arange(n)
        lo = np.maximum(0, idx - widths)
        out = (csum[idx + 1] - csum[lo]) / (idx + 1 - lo)
        env = np.sin(np.linspace(0, np.pi, n)) ** 2
        y = out * env
    else:
        raise ValueError(kind)
    y = y.astype(np.float32)
    return y / max(1e-6, float(np.max(np.abs(y))))


def sound(cfg, kind: str) -> np.ndarray:
    """Your own sound from brand/sfx/ if there is one, else the generated default."""
    folder = cfg.resolve(cfg.get("sfx.folder", "brand/sfx"))
    for ext in AUDIO_EXTS:
        p = folder / f"{kind}{ext}"
        if p.exists():
            try:
                return _decode_file(p)
            except Exception:
                log.warning("Couldn't read %s; using the built-in %s sound", p, kind)
    return synth(kind)


def _decode_file(path: Path) -> np.ndarray:
    import subprocess
    proc = subprocess.run([media._bin("ffmpeg"), "-hide_banner", "-loglevel", "error", "-i", str(path),
                           "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"], capture_output=True, timeout=60,
                          creationflags=media._CREATE_NO_WINDOW)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-300:])
    y = np.frombuffer(proc.stdout, dtype=np.float32)
    return y / max(1e-6, float(np.max(np.abs(y)))) if y.size else y


def events(recipe: dict, words: list[dict], duration: float, fps: int, hook_at: float | None) -> list[tuple[str, float]]:
    """(kind, time) for every effect on the output timeline, by the recipe's level."""
    level = recipe.get("sfx") or "off"
    if level not in ("light", "medium", "full"):
        return []
    out: list[tuple[str, float]] = []
    if hook_at is not None:
        out.append(("pop", max(0.0, hook_at)))
    if level in ("medium", "full") and recipe.get("zoom") == "punch":
        period = float(recipe.get("zoom_period", 6.0))
        t = period / 2
        while t < duration - 0.5:
            out.append(("whoosh", max(0.0, t - 0.12)))       # the swell peaks as the zoom lands
            t += period
    if level == "full":
        last = -10.0
        for w in words:
            if is_emphasis(w["w"]) and w["s"] - last >= 1.5 and w["s"] > 0.8:
                out.append(("tick", w["s"]))
                last = w["s"]
    return sorted((k, round(t, 3)) for k, t in out if t < duration)


def track(cfg, evts: list[tuple[str, float]], duration: float, out_path: Path) -> Path | None:
    """Write the effects as one mono 48 kHz WAV the length of the clip. None when there's nothing to add."""
    if not evts:
        return None
    gain = 10 ** (float(cfg.get("sfx.volume_db", -16)) / 20)
    n = int(duration * SR) + 1
    buf = np.zeros(n, dtype=np.float32)
    cache: dict[str, np.ndarray] = {}
    for kind, t in evts:
        s = cache.setdefault(kind, sound(cfg, kind))
        i = int(t * SR)
        if i >= n:
            continue
        seg = s[: n - i]
        buf[i: i + seg.size] += seg * gain
    pcm = (np.clip(buf, -1, 1) * 32767).astype("<i2")
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm.tobytes())
    return out_path
