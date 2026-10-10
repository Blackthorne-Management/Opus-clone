import json
import shutil
import sqlite3

import pytest

from kir_clipper import db, media
from kir_clipper.longform import (Timeline, apply_edits, keep_ranges, plan_segment, set_segments, suggest_segments,
                                  worker_tick)
from kir_clipper.pipeline import approve_clip, process_file
from kir_clipper.scheduler import copy_for

from .conftest import FakeLLM, FakeTranscriber


def words_with_gap(total=300.0, gap=(120.0, 330.0), step=0.4):
    out, t, n = [], 1.0, 0
    while t < total:
        if gap and gap[0] <= t < gap[1]:
            t = gap[1]
            continue
        n += 1
        out.append({"w": f"w{n}" + ("." if n % 10 == 0 else ""), "s": round(t, 2), "e": round(t + 0.3, 2)})
        t += step
    return out


def test_suggest_segments_splits_on_long_silence():
    words = words_with_gap(total=600, gap=(200, 420))
    segs = suggest_segments(words, 610, split_gap_s=180)
    assert len(segs) == 2
    assert segs[0]["end"] < 205 and segs[1]["start"] > 415
    assert [s["title"] for s in segs] == ["Part 1", "Part 2"]
    assert len(suggest_segments(words, 610, split_gap_s=600)) == 1


def test_keep_ranges_cuts_tightens_and_is_frame_exact():
    words = words_with_gap(total=200, gap=(100, 106))
    plan = {"cuts": [{"start": 40.0, "end": 55.0, "enabled": True}, {"start": 150, "end": 160, "enabled": False}]}
    keep = keep_ranges({"start": 0, "end": 200}, plan, words, tighten_over=3.0, keep_pause=0.8, fps=30)
    for k in keep:
        assert abs((k["e"] - k["s"]) * 30 - k["frames"]) < 1e-6
    covered = lambda t: any(k["s"] <= t < k["e"] for k in keep)
    assert not covered(47) and covered(39) and covered(56)
    assert not covered(103) and covered(100.2) and covered(106.1)   # 6s pause shortened
    assert covered(155)                                              # disabled cut is kept
    tl = Timeline(keep, intro_frames=120, fps=30)
    assert tl.map(0) == pytest.approx(4.0)
    assert tl.map(47) == tl.offsets[1]                               # inside a cut snaps forward
    assert tl.map(56) == pytest.approx(tl.offsets[1] + 1.0)


def test_plan_respects_confidence_and_budget(cfg):
    words = words_with_gap(total=1500, gap=None)
    plan = plan_segment(cfg, FakeLLM(), words, None, {"start": 0, "end": 1500})
    assert all(c["confidence"] >= 70 for c in plan["cuts"] if c["enabled"])
    assert any(not c["enabled"] for c in plan["cuts"])               # the 40% one is suggested but off
    removed = sum(c["end"] - c["start"] for c in plan["cuts"] if c["enabled"])
    assert removed <= 1500 * 0.25
    ts = [c["t"] for c in plan["chapters"]]
    assert all(b - a >= 240 for a, b in zip(ts, ts[1:]))
    assert all(len(p["text"].split()) <= 7 for p in plan["popups"])
    assert plan["meta"]["tags"] == ["kir", "mindset"]


def test_set_segments_validation(cfg, conn, tmp_path):
    sid = db.insert_stream(conn, tmp_path / "s.mp4", "hash-seg")
    with pytest.raises(ValueError):
        set_segments(conn, sid, [{"start": 0, "end": 100}, {"start": 50, "end": 200}], 300)
    with pytest.raises(ValueError):
        set_segments(conn, sid, [{"start": 0, "end": 10}], 300)
    ids = set_segments(conn, sid, [{"start": 150, "end": 290, "title": ""}, {"start": 0, "end": 140, "title": "Q&A"}], 300)
    rows = conn.execute("SELECT * FROM longform WHERE stream_id=? ORDER BY idx", (sid,)).fetchall()
    assert [r["id"] for r in rows] == ids and [r["title"] for r in rows] == ["Q&A", "Part 2"]
    assert all(r["status"] == "queued" for r in rows)


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("""CREATE TABLE clips (id INTEGER PRIMARY KEY, stream_id INTEGER, parent_clip_id INTEGER,
                   version INTEGER, start REAL, "end" REAL, score INTEGER, hook TEXT, reason TEXT, title TEXT,
                   caption TEXT, hashtags TEXT, platform_captions TEXT, recipe_json TEXT, file_path TEXT,
                   status TEXT, note TEXT, created_at TEXT, updated_at TEXT)""")
    old.execute("INSERT INTO clips(stream_id, start, \"end\", score, status) VALUES (1, 0, 1, 50, 'review')")
    old.commit()
    old.close()
    conn = db.connect(path)
    assert conn.execute("SELECT kind FROM clips").fetchone()[0] == "short"
    conn.execute("SELECT * FROM longform").fetchall()


def test_stream_to_youtube_long_form_end_to_end(cfg, conn, sample_video):
    cfg.data["longform"].update(width=640, height=360, fps=15)   # small output keeps the test fast
    shutil.copy(sample_video, cfg.path("inbox") / "stream.mp4")
    process_file(cfg, conn, cfg.path("inbox") / "stream.mp4", llm=FakeLLM(scores=(95,)), transcriber=FakeTranscriber())
    stream = conn.execute("SELECT * FROM streams").fetchone()
    rows = conn.execute("SELECT * FROM longform").fetchall()
    assert [r["status"] for r in rows] == ["needs_segments"]           # waits for the owner
    assert worker_tick(cfg, conn, llm=FakeLLM()) is None                # nothing queued yet

    set_segments(conn, stream["id"], [{"start": 0, "end": 75, "title": "First half"},
                                      {"start": 75, "end": stream["duration"], "title": "Second half"}],
                 stream["duration"])
    first = worker_tick(cfg, conn, llm=FakeLLM())
    second = worker_tick(cfg, conn, llm=FakeLLM())
    assert first and second and worker_tick(cfg, conn, llm=FakeLLM()) is None

    lf = conn.execute("SELECT * FROM longform ORDER BY idx").fetchall()
    assert [r["status"] for r in lf] == ["ready", "ready"]
    clip = db.clip_dict(db.get_clip(conn, first))
    assert clip["kind"] == "longform" and clip["status"] == "review"
    info = media.probe(clip["file_path"])
    assert (info["width"], info["height"]) == (640, 360) and info["has_audio"]
    plan = json.loads(lf[0]["plan_json"])
    removed = plan["render"]["removed_seconds"]
    assert removed > 0
    # intro + kept stream + outro
    assert info["duration"] == pytest.approx(4 + plan["render"]["kept_seconds"] + 8, abs=0.2)

    # Long-form never shows up in the shorts queue, and its copy has no #shorts.
    assert first not in [r["id"] for r in db.clips_with_status(conn, ["review"])]
    title, caption = copy_for(clip, "youtube")
    assert title == "Full stream: discipline" and "#shorts" not in caption

    # Untick a cut -> re-render queued without asking Claude again.
    calls = FakeLLM()
    assert apply_edits(cfg, conn, lf[0]["id"], {"cuts_enabled": {"0": False}})
    assert tuple(conn.execute("SELECT status, replan FROM longform WHERE id=?", (lf[0]["id"],)).fetchone()) == ("queued", 0)
    worker_tick(cfg, conn, llm=calls)
    assert calls.calls == []
    replan = json.loads(conn.execute("SELECT plan_json FROM longform WHERE id=?", (lf[0]["id"],)).fetchone()[0])
    assert replan["render"]["removed_seconds"] < removed

    approve_clip(cfg, conn, first)
    plats = [r["platform"] for r in conn.execute("SELECT platform FROM posts WHERE clip_id=?", (first,))]
    assert plats == ["youtube"]
    with pytest.raises(ValueError):
        apply_edits(cfg, conn, lf[0]["id"], {"rerender": True})
