"""Audio finishing for short clips: one fixed gain to -14 LUFS, then a look-ahead true-peak limiter.

From the master-skills /edit skill (Jake Schincariol, MIT). It replaces single-pass `loudnorm`, which
is dynamic: it rides the level like an automatic gain control, lifting room echo between words and
pumping on loud moments. Here the voice keeps its natural dynamics. The clip is measured once
(EBU R128, after the same 75 Hz high-pass the master uses), raised or lowered by one fixed amount,
and only the peaks are caught by the limiter at -1.5 dBTP. If reaching -14 would mean limiting the
peaks by more than `max_limiting_db`, the target drops to -16 LUFS so the voice isn't squashed.
"""

from __future__ import annotations

import functools
import logging
import math
import re
from pathlib import Path

from . import media

log = logging.getLogger("kir.audio")

PRE_FILTER = "highpass=f=75"


def parse_ebur128(stderr: str) -> dict:
    summary = stderr[stderr.rfind("Summary:"):] if "Summary:" in stderr else stderr
    out = {}
    for key, pattern in (("I", r"\bI:\s*(-?[\d.]+|-inf)\s*LUFS"), ("LRA", r"\bLRA:\s*(-?[\d.]+)\s*LU"),
                         ("TP", r"\bPeak:\s*(-?[\d.]+|-inf)\s*dBFS")):
        m = re.search(pattern, summary)
        if m:
            out[key] = float("-inf") if m.group(1) == "-inf" else float(m.group(1))
    return out


def measure(path: Path, pre_filter: str = PRE_FILTER) -> dict:
    """Integrated loudness (I, LUFS), loudness range (LRA) and true peak (TP, dBTP) of the first audio stream."""
    chain = (pre_filter + "," if pre_filter else "") + "ebur128=peak=true"
    proc = media.run([media._bin("ffmpeg"), "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0",
                      "-af", chain, "-f", "null", "-"])
    return parse_ebur128(proc.stderr)


@functools.lru_cache(maxsize=1)
def limiter_has_latency() -> bool:
    """alimiter's `latency` option (removes its 5 ms delay) is missing from old ffmpeg builds."""
    try:
        out = media.run([media._bin("ffmpeg"), "-hide_banner", "-h", "filter=alimiter"]).stdout
        return "latency" in out
    except Exception:
        return False


def gain_plan(measured: dict, target: float = -14.0, true_peak: float = -1.5, max_limiting_db: float = 6.0,
              fallback: float = -16.0, max_gain_db: float = 20.0) -> dict:
    """How much fixed gain to apply. Never more than max_gain_db (a near-silent clip stays quiet)."""
    loudness = measured.get("I")
    if loudness is None or not math.isfinite(loudness) or loudness < -60:
        return {"gain_db": 0.0, "target": None, "note": "no measurable speech; level left as recorded"}
    peak = measured.get("TP", loudness + 10)
    if not math.isfinite(peak):
        peak = loudness + 10
    chosen = target
    if peak + (target - loudness) - true_peak > max_limiting_db:
        chosen = fallback
    gain = max(-max_gain_db, min(max_gain_db, chosen - loudness))
    note = "" if chosen == target else (f"peaks would be limited by {peak + (target - loudness) - true_peak:.1f} dB "
                                       f"at {target} LUFS, so mastered to {fallback} LUFS")
    return {"gain_db": round(gain, 2), "target": chosen, "note": note}


def master_filter(gain_db: float, true_peak: float = -1.5) -> str:
    limit = round(10 ** (true_peak / 20), 4)
    lim = f"alimiter=limit={limit}:attack=5:release=50:level=false"
    tail = ""
    if limiter_has_latency():
        lim += ":latency=true"
    else:
        tail = ",atrim=start=0.005,asetpts=PTS-STARTPTS"  # compensate the limiter's look-ahead delay
    # Oversampled so the limiter catches inter-sample (true) peaks, not just sample peaks.
    return f"{PRE_FILTER},volume={gain_db:.2f}dB,aresample=192000,{lim},aresample=48000{tail}"


def finish(src: Path, dst: Path, cfg, bitrate: str = "192k") -> dict:
    """Copy the video, master the audio. `src` may carry raw PCM audio (one AAC encode, not two)."""
    a = cfg.get("audio") or {}
    target = float(a.get("target_lufs", -14.0))
    true_peak = float(a.get("true_peak", -1.5))
    plan = gain_plan(measure(src), target, true_peak, float(a.get("max_limiting_db", 6.0)),
                     float(a.get("fallback_lufs", -16.0)))

    def encode(gain: float) -> dict:
        media.ffmpeg("-i", src, "-map", "0:v:0?", "-map", "0:a:0", "-c:v", "copy",
                     "-af", master_filter(gain, true_peak), "-c:a", "aac", "-b:a", bitrate, "-ar", "48000",
                     "-movflags", "+faststart", dst)
        return measure(dst, pre_filter="")

    result = encode(plan["gain_db"])
    # The limiter takes a little loudness with the peaks it catches: measure again and correct once.
    miss = (plan["target"] - result["I"]) if plan["target"] is not None and math.isfinite(result.get("I", float("nan"))) else 0
    if abs(miss) > 0.5 and abs(plan["gain_db"] + miss) <= 20:
        plan["gain_db"] = round(plan["gain_db"] + miss, 2)
        result = encode(plan["gain_db"])
    plan["measured"] = {k: (round(v, 1) if math.isfinite(v) else None) for k, v in result.items()}
    if plan["note"]:
        log.info("Audio: %s", plan["note"])
    return plan
