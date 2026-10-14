"""Chunked transcription with word-level timestamps, plus a per-second loudness track.

Long streams are split into overlapping audio chunks so a 3-hour recording never has to sit
in memory at once. Words from the overlap zone are taken from whichever chunk saw them
closer to its centre, so nothing is duplicated or lost at the seams.
"""

from __future__ import annotations

import json
import logging
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


class _FasterWhisper:
    def __init__(self, cfg: Config):
        from faster_whisper import WhisperModel  # optional dependency

        device = cfg.get("transcription.device", "auto")
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
        self.language = cfg.get("transcription.language")

    def words(self, wav: Path) -> list[dict]:
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
        return json.loads(cache.read_text(encoding="utf-8"))

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
            for w in backend.words(wav):
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
    result = {"duration": duration, "language": cfg.get("transcription.language"), "words": words, "energy": energy}
    cache.write_text(json.dumps(result), encoding="utf-8")
    log.info("Transcript: %d words, cached as %s", len(words), cache.name)
    return result
