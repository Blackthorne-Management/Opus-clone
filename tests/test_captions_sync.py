"""Caption timing: word starts are re-timed to the audio, and the Hormozi style renders cleanly."""
import numpy as np

from kir_clipper.captions import build_ass, group_words
from kir_clipper.transcribe import align_word_starts

SR = 16000


def _speechlike(bursts, total=5.0, seed=1):
    rng = np.random.default_rng(seed)
    x = rng.normal(0, 0.003, int(total * SR))  # quiet room tone
    for a, b in bursts:
        i, j = int(a * SR), int(b * SR)
        x[i:j] += rng.normal(0, 0.25, j - i) * np.hanning(j - i) ** 0.2
    return x.astype(np.float32)


def test_early_word_starts_move_to_voice_onset():
    audio = _speechlike([(1.0, 1.4), (1.9, 2.6), (3.2, 3.6)])
    words = [
        {"w": "five", "s": 0.70, "e": 1.40},   # Whisper started it 300 ms early, in silence
        {"w": "years", "s": 2.05, "e": 2.60},  # already inside speech: must not move
        {"w": "later", "s": 2.85, "e": 3.60},  # 350 ms early after a pause
    ]
    assert align_word_starts(words, audio) == 2
    assert abs(words[0]["s"] - 0.98) < 0.04
    assert words[1]["s"] == 2.05
    assert abs(words[2]["s"] - 3.18) < 0.04


def test_alignment_never_moves_earlier_or_past_word_end():
    audio = _speechlike([(1.0, 1.5)])
    words = [{"w": "a", "s": 1.2, "e": 1.5}, {"w": "b", "s": 2.0, "e": 2.3}]  # "b" is entirely in silence
    align_word_starts(words, audio)
    assert words[0]["s"] == 1.2 and words[1]["s"] == 2.0


def test_flat_audio_is_left_alone():
    audio = np.random.default_rng(0).normal(0, 0.1, SR * 3).astype(np.float32)
    words = [{"w": "x", "s": 0.5, "e": 0.9}]
    assert align_word_starts(words, audio) == 0 and words[0]["s"] == 0.5


def _events(ass):
    return [l for l in ass.splitlines() if l.startswith("Dialogue: 0,")]


def _t(ts):
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def test_hormozi_phrases_are_short_held_and_slightly_early():
    words, t = [], 1.0
    for x in "five years to make $5 million most people never".split():
        words.append({"w": x, "s": t, "e": t + 0.28}); t += 0.32
    ev = _events(build_ass(words, 6, {"caption_style": "hormozi"}, {}))
    assert ev, "no caption events"
    starts = [_t(e.split(",")[1]) for e in ev]
    ends = [_t(e.split(",")[2]) for e in ev]
    assert abs(starts[0] - 0.96) < 0.011                    # 40 ms lead
    assert all(abs(ends[i] - starts[i + 1]) < 0.011 for i in range(len(ev) - 1))  # no flicker gaps
    assert all(len(g) <= 3 and len(" ".join(w["w"] for w in g)) <= 13
               for g in group_words(words, 3, 0.35, 13))
    money = next(e for e in ev if "$5" in e)
    assert "&H4AFF2B&}$5" in money or "&H4AFF2B&\\fscx122" in money  # money in green


def test_hormozi_long_word_shrinks_and_real_pause_clears_screen():
    words = [{"w": "entrepreneurship", "s": 1.0, "e": 1.8}, {"w": "matters", "s": 3.5, "e": 3.9}]
    ev = _events(build_ass(words, 5, {"caption_style": "hormozi"}, {}))
    assert "\\fs" in ev[0]                       # shrunk to fit one line
    assert _t(ev[0].split(",")[2]) < 2.1         # 1.7 s pause: caption leaves the screen
