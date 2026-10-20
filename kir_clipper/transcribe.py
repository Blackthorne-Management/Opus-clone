"""Chunked transcription with word-level timestamps, plus a per-second loudness track.

Long streams are split into overlapping audio chunks so a 3-hour recording never has to sit
in memory at once. Words from the overlap zone are taken from whichever chunk saw them
closer to its centre, so nothing is duplicated or lost at the seams.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

from . import media
from .config import Config, secret

log = logging.getLogger("kir.transcribe")


def plan_chunks(duration: float, chunk_s: float, overlap_s: float) -> list[tuple[float, float, float, float]]:
    """Return (audio_start, audio_len, keep_from, keep_to) per chunk in absolute seconds."""
    if duration <= chunk_s:
        return [(0.0, duration, 0.0, duration)]
    chunks = []
    start = 0.0
    while start < duration:
        a_start = max(0.0, start - overlap_s)
        a_end = min(duration, start + chunk_s + overlap_s)
        keep_from = start
        keep_to = min(duration, start + chunk_s)
        chunks.append((a_start, a_end - a_start, keep_from, keep_to))
        start += chunk_s
    return chunks


def add_nvidia_dll_dirs() -> list[str]:
    """Windows: make pip-installed CUDA libraries (nvidia-cublas-cu12, nvidia-cudnn-cu12) loadable.

    pip puts cublas64_12.dll etc. in site-packages/nvidia/*/bin, which Windows never searches, so
    CTranslate2 fails mid-transcription with "cublas64_12.dll is not found". Add those folders to the
    DLL search path and PATH before the model loads.
    """
    if sys.platform != "win32":
        return []
    added = []
    for base in map(Path, sys.path):
        nv = base / "nvidia"
        if not nv.is_dir():
            continue
        for bin_dir in sorted(nv.glob("*/bin")):
            d = str(bin_dir)
            if d in added:
                continue
            try:
                os.add_dll_directory(d)
            except OSError:
                continue
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            added.append(d)
    return added


def _is_gpu_library_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("cublas", "cudnn", "cuda", ".dll", "libcu"))


class _FasterWhisper:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.language = cfg.get("transcription.language")
        if add_nvidia_dll_dirs():
            log.debug("Added NVIDIA library folders to the DLL search path")
        self._load(cfg.get("transcription.device", "auto"))

    def _load(self, device: str) -> None:
        from faster_whisper import WhisperModel  # optional dependency

        cfg = self.cfg
        compute = cfg.get("transcription.compute_type", "auto")
        if device == "auto":
            try:
                import ctranslate2

                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception:
                device = "cpu"
        if compute == "auto":
            compute = "float16" if device == "cuda" else "int8"
        log.info("Loading faster-whisper %s on %s (%s)", cfg.get("transcription.model"), device, compute)
        self.model = WhisperModel(cfg.get("transcription.model", "large-v3"), device=device, compute_type=compute)
        self.device = device

    def words(self, wav: Path) -> list[dict]:
        try:
            return self._words(wav)
        except RuntimeError as exc:
            if self.device != "cuda" or not _is_gpu_library_error(exc):
                raise
            log.warning("GPU transcription failed (%s). Falling back to CPU for this run; it is slower. "
                        "Fix: .venv\\Scripts\\python -m pip install nvidia-cublas-cu12 \"nvidia-cudnn-cu12==9.*\"", exc)
            self._load("cpu")
            return self._words(wav)

    def _words(self, wav: Path) -> list[dict]:
        # Pass decoded samples, not a path: faster-whisper's own file decoding goes through PyAV,
        # whose releases have broken it (PyAV 19 dropped an argument it passes).
        segments, _info = self.model.transcribe(
            read_wav(wav), language=self.language, word_timestamps=True, vad_filter=True,
            condition_on_previous_text=False,
        )
        out = []
        for seg in segments:
            for w in seg.words or []:
                out.append({"w": w.word.strip(), "s": float(w.start), "e": float(w.end), "p": float(w.probability)})
        return out


class _OpenAIWhisper:
    """Whisper API. Each chunk is re-encoded to mono 64 kbps MP3 to stay under the 25 MB upload limit."""

    def __init__(self, cfg: Config):
        from openai import OpenAI  # optional dependency

        if not secret("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set but transcription.backend is 'openai'")
        self.client = OpenAI()
        self.language = cfg.get("transcription.language")

    def words(self, wav: Path) -> list[dict]:
        mp3 = wav.with_suffix(".mp3")
        media.ffmpeg("-i", wav, "-ac", "1", "-b:a", "64k", mp3)
        with open(mp3, "rb") as fh:
            resp = self.client.audio.transcriptions.create(
                model="whisper-1", file=fh, language=self.language,
                response_format="verbose_json", timestamp_granularities=["word"],
            )
        return [{"w": w.word.strip(), "s": float(w.start), "e": float(w.end), "p": 1.0} for w in (resp.words or [])]


def make_backend(cfg: Config):
    backend = cfg.get("transcription.backend", "faster-whisper")
    if backend == "openai":
        return _OpenAIWhisper(cfg)
    return _FasterWhisper(cfg)


def read_wav(wav: Path) -> np.ndarray:
    """A 16 kHz mono 16-bit WAV (from media.extract_audio) as float32 samples in [-1, 1]."""
    with wave.open(str(wav), "rb") as wf:
        if wf.getframerate() != 16000 or wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise ValueError(f"{wav.name}: expected 16 kHz mono 16-bit audio")
        frames = wf.readframes(wf.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


ALIGN_VERSION = 2
_FRAME = 160  # 10 ms at 16 kHz


def align_word_starts(words: list[dict], samples: np.ndarray, sr: int = 16000, max_shift: float = 0.6) -> int:
    """Move word starts that Whisper placed in silence forward to where the voice actually begins.

    Whisper often starts a word early, pulling it back into the pause before it, so captions
    flash a word before it is said. Starts are only ever moved later, never past the word's own
    end, and only when the original start sits in quiet audio. Times are relative to `samples`.
    Returns how many words were moved.
    """
    n = len(samples) // _FRAME
    if n < 10 or not words:
        return 0
    frames = samples[: n * _FRAME].reshape(n, _FRAME)
    db = 20 * np.log10(np.maximum(np.sqrt(np.mean(frames ** 2, axis=1)), 1e-5))
    noise, loud = np.percentile(db, 10), np.percentile(db, 95)
    if loud - noise < 12:  # no usable contrast between speech and background
        return 0
    thr = noise + 0.35 * (loud - noise)
    voiced = db >= thr
    moved = _snap_boundaries(words, db, sr)
    prev_s = -1.0
    for w in words:
        f0 = int(w["s"] * sr / _FRAME)
        if 0 <= f0 < n - 3 and not voiced[f0: f0 + 3].any():
            limit = min(n - 1, int(min(w["e"] - 0.08, w["s"] + max_shift) * sr / _FRAME))
            for f in range(f0 + 1, limit):
                if voiced[f] and voiced[f + 1]:
                    new_s = max(w["s"], f * _FRAME / sr - 0.02, prev_s + 0.05)
                    if new_s < w["e"] - 0.05:
                        w["s"] = round(new_s, 3)
                        moved += 1
                    break
        prev_s = w["s"]
    return moved


def _snap_boundaries(words: list[dict], db: np.ndarray, sr: int, window: float = 0.12, min_dip_db: float = 3.0) -> int:
    """In connected speech Whisper's word boundaries can be off by 100-200 ms either way. Between two
    words that run together, the real boundary is usually the quietest moment near it, so move the
    boundary to the energy dip within +/-window seconds (only if the dip is clearly quieter)."""
    n = len(db)
    smooth = np.convolve(db, np.ones(3) / 3, mode="same")
    fps = sr / _FRAME
    moved = 0
    for a, b in zip(words, words[1:]):
        if b["s"] - a["e"] > 0.15:  # a real pause: handled by the onset pass
            continue
        t = b["s"]
        lo = int(max(t - window, a["s"] + 0.06) * fps)
        hi = int(min(t + window, b["e"] - 0.06) * fps)
        f0 = int(t * fps)
        if not (0 <= lo < f0 < hi < n):
            continue
        f = lo + int(np.argmin(smooth[lo:hi + 1]))
        if smooth[f0] - smooth[f] >= min_dip_db and abs(f - f0) >= 2:
            new_t = round(f / fps, 3)
            b["s"] = new_t
            a["e"] = min(a["e"], new_t) if new_t > a["s"] + 0.05 else a["e"]
            moved += 1
    return moved


def loudness_track(wav: Path) -> list[float]:
    """RMS loudness in dBFS for each second of a 16 kHz mono 16-bit WAV."""
    with wave.open(str(wav), "rb") as wf:
        rate = wf.getframerate()
        out = []
        while True:
            frames = wf.readframes(rate)
            if not frames:
                break
            samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
            out.append(round(20 * np.log10(max(rms, 1e-5)), 1))
    return out


def transcribe(cfg: Config, source: Path, file_hash: str, duration: float, backend=None) -> dict:
    """Transcribe `source`, caching the result by content hash so reruns are free."""
    cache = cfg.subdir("transcripts") / f"{file_hash}.json"
    if cache.exists():
        log.info("Using cached transcript %s", cache.name)
        result = json.loads(cache.read_text(encoding="utf-8"))
        if result.get("aligned", 0) < ALIGN_VERSION:
            _realign(cfg, source, duration, result)
            cache.write_text(json.dumps(result), encoding="utf-8")
        return result

    backend = backend or make_backend(cfg)
    chunk_s = float(cfg.get("transcription.chunk_minutes", 20)) * 60
    overlap_s = float(cfg.get("transcription.overlap_seconds", 20))
    words: list[dict] = []
    energy: list[float] = []

    with tempfile.TemporaryDirectory(prefix="kir_audio_") as tmp:
        plan = plan_chunks(duration, chunk_s, overlap_s)
        for i, (a_start, a_len, keep_from, keep_to) in enumerate(plan):
            wav = Path(tmp) / f"chunk_{i:03d}.wav"
            log.info("Transcribing chunk %d/%d (%.0f-%.0fs)", i + 1, len(plan), keep_from, keep_to)
            media.extract_audio(source, wav, a_start, a_len)
            last = i == len(plan) - 1
            chunk_words = backend.words(wav)
            align_word_starts(chunk_words, read_wav(wav))
            for w in chunk_words:
                w["s"] = round(w["s"] + a_start, 3)
                w["e"] = round(w["e"] + a_start, 3)
                if keep_from <= w["s"] and (w["s"] < keep_to or last):
                    words.append(w)
            # Loudness: only the seconds this chunk "owns", to avoid double counting.
            track = loudness_track(wav)
            skip = int(round(keep_from - a_start))
            energy.extend(track[skip: skip + int(round(keep_to - keep_from))])
            wav.unlink(missing_ok=True)

    words.sort(key=lambda w: w["s"])
    result = {"duration": duration, "language": cfg.get("transcription.language"), "words": words, "energy": energy,
              "aligned": ALIGN_VERSION}
    cache.write_text(json.dumps(result), encoding="utf-8")
    log.info("Transcript: %d words, cached as %s", len(words), cache.name)
    return result


def _realign(cfg: Config, source: Path, duration: float, result: dict) -> None:
    """Apply word-start alignment to a transcript cached before alignment existed (no re-transcribing)."""
    words = result["words"]
    chunk_s = float(cfg.get("transcription.chunk_minutes", 20)) * 60
    moved = 0
    with tempfile.TemporaryDirectory(prefix="kir_align_") as tmp:
        for i, (a_start, a_len, keep_from, keep_to) in enumerate(plan_chunks(duration, chunk_s, 0.0)):
            last = keep_to >= duration
            part = [w for w in words if keep_from <= w["s"] and (w["s"] < keep_to or last)]
            if not part:
                continue
            wav = Path(tmp) / f"align_{i:03d}.wav"
            media.extract_audio(source, wav, a_start, a_len)
            rel = [{**w, "s": w["s"] - a_start, "e": w["e"] - a_start} for w in part]
            moved += align_word_starts(rel, read_wav(wav))
            for w, r in zip(part, rel):
                w["s"] = round(r["s"] + a_start, 3)
                w["e"] = round(r["e"] + a_start, 3)
            wav.unlink(missing_ok=True)
    result["aligned"] = ALIGN_VERSION
    log.info("Re-timed %d of %d cached words to the audio", moved, len(words))
