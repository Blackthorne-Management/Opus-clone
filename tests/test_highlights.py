from kir_clipper.highlights import build_sentences, dedupe, find_highlights, overlap_ratio, snap, windows
from kir_clipper.pipeline import gate
from kir_clipper.transcribe import plan_chunks

from .conftest import FakeLLM


def words_every(n, step=0.4, sentence_every=10, gap_at=None):
    out, t = [], 0.0
    for i in range(1, n + 1):
        if gap_at and i == gap_at:
            t += 2.0
        out.append({"w": f"w{i}" + ("." if i % sentence_every == 0 else ""), "s": round(t, 2), "e": round(t + 0.3, 2)})
        t += step
    return out


def test_sentences_split_on_punctuation_and_pauses():
    s = build_sentences(words_every(25, gap_at=15))
    assert [x["text"].split()[-1] for x in s] == ["w10.", "w14", "w20.", "w25"]
    assert all(a["end"] <= b["start"] for a, b in zip(s, s[1:]))


def test_dedupe_keeps_higher_score_when_overlap_over_half():
    a = {"start": 0, "end": 40, "score": 80}
    b = {"start": 15, "end": 55, "score": 92}   # 25s overlap of 40s = 62%
    c = {"start": 35, "end": 75, "score": 70}   # overlaps b by 20/40 = 50%: kept
    assert overlap_ratio(a, b) > 0.5
    kept = dedupe([a, b, c], 0.5)
    assert b in kept and a not in kept and c in kept


def test_snap_lands_on_word_edges_without_clipping_neighbours():
    w = words_every(40)
    s = build_sentences(w)
    clip = snap({"start_idx": 1, "end_idx": 2}, s, w, lead_in=0.15, tail=0.45)
    assert clip["start"] > w[9]["e"]           # after previous word ends
    assert clip["start"] <= w[10]["s"]
    assert clip["end"] >= w[29]["e"] and clip["end"] < w[30]["s"]


def test_windows_cover_everything():
    s = build_sentences(words_every(3000))
    wins = windows(s, 300, 30)
    covered = {x["i"] for win in wins for x in win}
    assert covered == {x["i"] for x in s}


def test_chunk_plan_owns_every_second_once():
    plan = plan_chunks(3 * 3600, 1200, 20)
    assert plan[0][2] == 0 and plan[-1][3] == 3 * 3600
    for (_, _, _, end), (_, _, start, _) in zip(plan, plan[1:]):
        assert end == start


def test_find_highlights_respects_cap_and_length(cfg):
    words = words_every(4000)   # ~27 minutes
    transcript = {"words": words, "energy": [-30.0] * 1700}
    capped = cfg
    capped.data["highlights"]["max_clips_per_stream"] = 5
    clips = find_highlights(capped, transcript, FakeLLM())
    assert 0 < len(clips) <= 5
    for c in clips:
        assert cfg.get("highlights.min_seconds") <= c["end"] - c["start"] <= cfg.get("highlights.max_seconds")
        assert {"score", "hook", "reason", "title", "caption", "hashtags", "platform_captions"} <= c.keys()
    assert clips == sorted(clips, key=lambda c: c["start"])


def test_gate_thresholds(cfg):
    assert gate(95, cfg) == "auto" and gate(90, cfg) == "auto"
    assert gate(89, cfg) == "review" and gate(60, cfg) == "review"
    assert gate(59, cfg) == "drop"
