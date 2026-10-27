"""Tight jump cuts inside a clip: dead air, filler words ("um", "uh") and retakes.

Ported from the dead-space cutter in the master-skills /edit skill (Jake Schincariol, MIT):
  - a 10 ms speech envelope with an adaptive gate finds pauses; each cut keeps a breath pad on
    both sides so words are never clipped, and never cuts through a transcribed word that has energy;
  - filler words come from the transcript (Whisper drops some, so not every "um" is caught);
  - a retake is a line said twice in a row: the abandoned attempt is cut, the last take wins;
  - keep ranges snap to the output frame grid so audio and video stay in sync, and audio joins get
    short equal-power crossfades (no clicks).

When the speech gate is unreliable (music or game audio under the voice), pauses are found from the
gaps between transcribed words instead. A safety cap stops a clip from losing too much: if the plan
would remove more than `max_cut_ratio` of it through cuts that could take words with them (fillers,
retakes, pauses guessed from word timings), retakes are dropped first, then the pace loosens, then the
clip is left uncut. Dead air found by a reliable level gate doesn't count toward the cap.
"""

from __future__ import annotations

import difflib
import logging
import math
import re
import tempfile
from pathlib import Path

import numpy as np

from . import media

log = logging.getLogger("kir.jumpcut")

PACES = {  # min_silence, pad_before, pad_after, min_cut (seconds)
    "tight": (0.25, 0.06, 0.10, 0.10),
    "normal": (0.35, 0.08, 0.14, 0.12),
    "loose": (0.60, 0.12, 0.22, 0.20),
}
PACE_NAMES = ("off",) + tuple(PACES)
FILLERS = frozenset("um umm ummm uh uhh uhm er erm ah ahh hmm hm mm mmm".split())
HOP = 0.01
SR = 16000


# ---------------------------------------------------------------- audio

def envelope(samples: np.ndarray, sr: int = SR, hop: float = HOP) -> list[float]:
    """Short-term level in dBFS every `hop` seconds. Each frame's own mean is removed first, so DC
    offset and the slowest rumble don't count as sound."""
    n = int(round(sr * hop))
    frames = samples.size // n
    if frames == 0:
        return []
    power = np.var(samples[: frames * n].astype(np.float64).reshape(frames, n), axis=1)
    return [float(v) for v in 10 * np.log10(np.maximum(power, 1e-10))]


def _percentile(values: list[float], fraction: float) -> float:
    pos = min(len(values) - 1, max(0, int(round(fraction * (len(values) - 1)))))
    return values[pos]


def speech_levels(levels: list[float]) -> dict:
    """Adaptive gate: noise floor = 10th percentile, speech = loud frames. The gate sits 30 dB under
    speech but never closer than 8 dB to the noise floor, so quiet takes and noisy rooms both work."""
    values = sorted(levels)
    if not values:
        return {"noise_db": -100.0, "speech_db": -100.0, "threshold_db": -100.0, "spread_db": 0.0, "reliable": False}
    noise = _percentile(values, 0.10)
    speech = max(_percentile(values, 0.95), _percentile(values, 0.99) - 6.0, values[-1] - 12.0)
    spread = speech - noise
    threshold = max(noise + 8.0, speech - 30.0) if spread >= 18.0 else noise + spread * 0.5
    return {"noise_db": round(noise, 1), "speech_db": round(speech, 1), "threshold_db": round(threshold, 1),
            "spread_db": round(spread, 1), "reliable": spread >= 12.0}


def find_silences(levels: list[float], hop: float, threshold: float, min_sound: float = 0.06):
    """(silent runs as (start, end) seconds, first sound, last sound)."""
    loud = [v >= threshold for v in levels]
    runs, i = [], 0
    while i < len(loud):
        j = i
        while j < len(loud) and loud[j] == loud[i]:
            j += 1
        runs.append([loud[i], i, j])
        i = j
    for k, run in enumerate(runs):  # a click shorter than min_sound inside silence is still silence
        if run[0] and 0 < k < len(runs) - 1 and (run[2] - run[1]) * hop < min_sound:
            run[0] = False
    merged = []
    for run in runs:
        if merged and merged[-1][0] == run[0]:
            merged[-1][2] = run[2]
        else:
            merged.append(run)
    sound = [r for r in merged if r[0]]
    if not sound:
        return [], None, None
    return [(r[1] * hop, r[2] * hop) for r in merged if not r[0]], sound[0][1] * hop, sound[-1][2] * hop


# ---------------------------------------------------------------- cuts

def norm_token(text: str) -> str:
    return re.sub(r"[^\w']+", "", text.lower()).strip("'")


def merge_cuts(cuts: list[dict]) -> list[dict]:
    out = []
    for cut in sorted(cuts, key=lambda c: c["start"]):
        if out and cut["start"] <= out[-1]["end"] + 1e-6:
            last = out[-1]
            last["end"] = max(last["end"], cut["end"])
            reasons = last["reason"].split("+")
            if cut["reason"] not in reasons:
                last["reason"] = "+".join(reasons + [cut["reason"]])
            if cut.get("text"):
                last["text"] = ((last.get("text") or "") + " " + cut["text"]).strip()
        else:
            out.append(dict(cut))
    return out


def keeps_from_cuts(cuts: list[dict], start: float, end: float) -> list[list[float]]:
    keeps, cursor = [], start
    for cut in merge_cuts(cuts):
        cs, ce = max(start, cut["start"]), min(end, cut["end"])
        if ce <= cs:
            continue
        if cs > cursor:
            keeps.append([cursor, cs])
        cursor = max(cursor, ce)
    if end > cursor:
        keeps.append([cursor, end])
    return keeps


def _edges(words, k, pad_before, pad_after, t0, t1):
    """Cut boundaries in the gap before word k; pads shrink to half the gap when words are close."""
    if k <= 0:
        return t0, max(t0, words[0]["s"] - pad_before)
    if k >= len(words):
        return min(t1, words[-1]["e"] + pad_after), t1
    prev_end, start = words[k - 1]["e"], words[k]["s"]
    gap = max(0.0, start - prev_end)
    return prev_end + min(pad_after, gap / 2), start - min(pad_before, gap / 2)


def transcript_cuts(words, fillers, retakes, pad_before, pad_after, t0, t1) -> list[dict]:
    cuts = []
    for k, word in enumerate(words):
        if fillers and norm_token(word["w"]) in fillers:
            left, _ = _edges(words, k, pad_before, pad_after, t0, t1)
            _, right = _edges(words, k + 1, pad_before, pad_after, t0, t1)
            if right > left:
                cuts.append({"start": left, "end": right, "reason": "filler", "text": word["w"]})
    if retakes == "off":
        return cuts
    idx = [k for k, w in enumerate(words) if norm_token(w["w"]) and norm_token(w["w"]) not in FILLERS]
    tokens = [norm_token(words[k]["w"]) for k in idx]
    j = 1
    while j < len(tokens):
        found = None
        for length in range(min(25, j), 1, -1):  # longest abandoned attempt first
            a, b = tokens[j - length:j], tokens[j:j + length]
            first, restart = words[idx[j - length]], words[idx[j]]
            if restart["s"] - first["s"] > 15.0:
                continue
            if a == b:
                found = length
                break
            if (retakes == "fuzzy" and length >= 4 and len(b) == length and a[:2] == b[:2]
                    and restart["s"] - words[idx[j - 1]]["e"] >= 0.25
                    and difflib.SequenceMatcher(None, a, b).ratio() >= 0.8):
                found = length
                break
        if found:
            left, _ = _edges(words, idx[j - found], pad_before, pad_after, t0, t1)
            _, right = _edges(words, idx[j], pad_before, pad_after, t0, t1)
            text = " ".join(words[idx[m]]["w"] for m in range(j - found, j))
            if right > left:
                cuts.append({"start": left, "end": right, "reason": "retake", "text": text})
        j += 1
    return cuts


def silence_cuts(levels, stats, words, duration, pace) -> list[dict]:
    min_silence, pad_before, pad_after, min_cut = PACES[pace]
    cuts = []
    if stats["reliable"]:
        silences, first, last = find_silences(levels, HOP, stats["threshold_db"])
        if first is None:
            return []
        spans = [(s, e) for s, e in silences if s > first and e < last]
        # Dead air before the first and after the last sound.
        if first - pad_before >= min_cut:
            cuts.append({"start": 0.0, "end": first - pad_before, "reason": "silence", "text": None})
        if duration - (last + pad_after) >= min_cut:
            cuts.append({"start": last + pad_after, "end": duration, "reason": "silence", "text": None})
    else:
        # Music or game audio under the voice: trust the word timings instead of the level gate.
        spans = [(a["e"], b["s"]) for a, b in zip(words, words[1:])]
    for s, e in spans:
        if e - s < min_silence:
            continue
        cs, ce = s + pad_after, e - pad_before
        if ce - cs >= min_cut:
            cuts.append({"start": cs, "end": ce, "reason": "silence", "text": None})
    if not stats["reliable"] or not words:
        return cuts
    # Never cut through a transcribed word that has real energy (the gate can miss soft consonants).
    guarded = []
    for cut in cuts:
        pieces = [[cut["start"], cut["end"]]]
        for w in words:
            core_s, core_e = w["s"] + 0.06, w["e"] - 0.06
            if core_e <= core_s or norm_token(w["w"]) in FILLERS:
                continue
            lo, hi = int(core_s / HOP), int(core_e / HOP) + 1
            seg = levels[max(0, lo):hi]
            if not seg or max(seg) < stats["noise_db"] + 6:
                continue
            nxt = []
            for ps, pe in pieces:
                if core_e <= ps or core_s >= pe:
                    nxt.append([ps, pe])
                    continue
                if core_s - ps >= min_cut:
                    nxt.append([ps, core_s])
                if pe - core_e >= min_cut:
                    nxt.append([core_e, pe])
            pieces = nxt
        guarded += [dict(cut, start=ps, end=pe) for ps, pe in pieces]
    return guarded


def snap(keeps: list[list[float]], fps: int, duration: float, min_keep: float) -> list[list[int]]:
    """Keep ranges as whole output frames [k, m); slivers shorter than min_keep merge into a neighbour."""
    total = int(math.floor(duration * fps + 1e-6))
    frames = []
    for s, e in keeps:
        k = max(0, int(math.floor(s * fps + 1e-6)))
        m = min(total, int(math.ceil(e * fps - 1e-6)))
        if m <= k:
            continue
        if frames and k <= frames[-1][1]:
            frames[-1][1] = max(frames[-1][1], m)
        else:
            frames.append([k, m])
    min_frames = max(2, int(math.ceil(min_keep * fps)))
    changed = True
    while changed and len(frames) > 1:
        changed = False
        for i, (k, m) in enumerate(frames):
            if m - k >= min_frames:
                continue
            gap_l = k - frames[i - 1][1] if i > 0 else None
            gap_r = frames[i + 1][0] - m if i + 1 < len(frames) else None
            if gap_r is None or (gap_l is not None and gap_l <= gap_r):
                frames[i - 1][1] = m
            else:
                frames[i + 1][0] = k
            del frames[i]
            changed = True
            break
    return frames


def remap_words(words: list[dict], keeps: list[list[float]]) -> list[dict]:
    """Move word times onto the cut timeline; words inside cuts are dropped."""
    out, offset, segments = [], 0.0, []
    for start, end in keeps:
        segments.append((start, end, offset))
        offset += end - start
    for word in words:
        mid = (word["s"] + word["e"]) / 2
        for start, end, out_start in segments:
            if start <= mid < end:
                ws = out_start + max(word["s"], start) - start
                we = out_start + min(word["e"], end) - start
                out.append({**word, "s": round(ws, 3), "e": round(max(we, ws + 0.01), 3)})
                break
    return out


# ---------------------------------------------------------------- plan

def settings_for(cfg, recipe: dict) -> dict | None:
    """The jump-cut settings for this render, or None when cutting is off."""
    pace = recipe.get("jumpcut")
    if pace is None:
        pace = cfg.get("jumpcut.pace", "tight") if cfg.get("jumpcut.enabled", True) else "off"
    if pace not in PACES:
        return None
    return {"pace": pace,
            "fillers": bool(cfg.get("jumpcut.fillers", True)),
            "retakes": str(cfg.get("jumpcut.retakes", "fuzzy")),
            "max_cut_ratio": float(cfg.get("jumpcut.max_cut_ratio", 0.4)),
            "crossfade_ms": float(cfg.get("jumpcut.crossfade_ms", 30))}


def plan_from_levels(levels: list[float], words: list[dict], duration: float, fps: int, settings: dict) -> dict | None:
    """Pure planning step (no ffmpeg), so it can be tested directly. Times are relative to the clip.

    Returns {"keeps": [[s, e]], "frames": [[k, m]], "cuts": [...], "removed": seconds, "pace": name}
    or None when nothing worth cutting was found."""
    stats = speech_levels(levels) if levels else speech_levels([])
    words = [w for w in words if w["e"] > 0 and w["s"] < duration]
    attempts = [(settings["pace"], settings["retakes"]), (settings["pace"], "off"), ("loose", "off")]
    for pace, retakes in attempts:
        _, pad_before, pad_after, min_cut = PACES[pace]
        cuts = silence_cuts(levels, stats, words, duration, pace)
        cuts += transcript_cuts(words, FILLERS if settings["fillers"] else frozenset(), retakes,
                                pad_before, pad_after, 0.0, duration)
        cuts = [c for c in merge_cuts(cuts) if c["end"] - c["start"] >= min_cut]
        if not cuts:
            return None
        frames = snap(keeps_from_cuts(cuts, 0.0, duration), fps, duration, min_keep=0.12)
        if not frames:
            continue
        keeps = [[k / fps, m / fps] for k, m in frames]
        kept = sum(e - s for s, e in keeps)
        removed = duration - kept
        # Dead air found by a reliable level gate is safe to remove however much there is. The cap is for
        # cuts that could take words with them: fillers, retakes, and pauses guessed from word timings.
        risky = sum(c["end"] - c["start"] for c in cuts if not (stats["reliable"] and c["reason"] == "silence"))
        if risky <= duration * settings["max_cut_ratio"] and kept >= 3.0:
            return {"keeps": keeps, "frames": frames, "cuts": cuts, "removed": round(removed, 2), "pace": pace,
                    "gate": "level" if stats["reliable"] else "words"}
        log.info("Jump cuts at %s/%s would remove %.1fs of %.1fs (%.1fs of it speech-adjacent); easing off",
                 pace, retakes, removed, duration, risky)
    return None


def plan(source: Path, start: float, duration: float, words: list[dict], fps: int, settings: dict) -> dict | None:
    """Plan the cuts for one clip of `source` (words relative to `start`)."""
    from .transcribe import read_wav
    with tempfile.TemporaryDirectory(prefix="kir_jump_") as tmp:
        wav = media.extract_audio(source, Path(tmp) / "clip.wav", start, duration)
        levels = envelope(read_wav(wav))
    result = plan_from_levels(levels, words, duration, fps, settings)
    if result:
        reasons = {}
        for c in result["cuts"]:
            for r in c["reason"].split("+"):
                reasons[r] = reasons.get(r, 0) + 1
        log.info("Jump cuts (%s pace, %s gate): %d cuts %s, %.1fs removed", result["pace"], result["gate"],
                 len(result["cuts"]), reasons, result["removed"])
    return result


# ---------------------------------------------------------------- ffmpeg graph

def video_chain(frames: list[list[int]], fps: int, src: str = "0:v", out: str = "cut") -> str:
    n = len(frames)
    if n == 1:
        k, m = frames[0]
        return f"[{src}]fps={fps},trim=start_frame={k}:end_frame={m},setpts=PTS-STARTPTS[{out}]"
    parts = [f"[{src}]fps={fps},split={n}" + "".join(f"[js{i}]" for i in range(n))]
    for i, (k, m) in enumerate(frames):
        parts.append(f"[js{i}]trim=start_frame={k}:end_frame={m},setpts=PTS-STARTPTS[jv{i}]")
    parts.append("".join(f"[jv{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[{out}]")
    return ";".join(parts)


def audio_chain(keeps: list[list[float]], crossfade_ms: float, src: str = "0:a:0", out: str = "acut") -> str:
    """atrim each keep (overlapping by half a crossfade on inner edges), then equal-power crossfades.
    The crossfades eat exactly the overlap, so audio length equals the video's."""
    xf = crossfade_ms / 1000.0
    n = len(keeps)
    parts = [f"[{src}]asplit={n}" + "".join(f"[ja{i}]" for i in range(n))] if n > 1 else []
    for i, (s, e) in enumerate(keeps):
        s2 = max(0.0, s - (xf / 2 if i > 0 else 0))
        e2 = e + (xf / 2 if i < n - 1 else 0)
        parts.append(f"[{'ja' + str(i) if n > 1 else src}]atrim=start={s2:.6f}:end={e2:.6f},asetpts=PTS-STARTPTS[jt{i}]")
    cur = "jt0"
    for i in range(1, n):
        parts.append(f"[{cur}][jt{i}]acrossfade=d={xf:.4f}:c1=qsin:c2=qsin[jx{i}]")
        cur = f"jx{i}"
    parts.append(f"[{cur}]anull[{out}]")
    return ";".join(parts)
