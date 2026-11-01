import json
import subprocess

from kir_clipper import audio, jumpcut, media, stopmotion
from kir_clipper.render import default_recipe, edit_report, render_clip

SETTINGS = {"pace": "tight", "fillers": True, "retakes": "fuzzy", "max_cut_ratio": 0.4, "crossfade_ms": 30}


def levels_for(spans, duration, loud=-20.0, quiet=-70.0):
    """10 ms levels: loud inside the given (start, end) spans, quiet elsewhere."""
    out = []
    for i in range(int(duration / jumpcut.HOP)):
        t = i * jumpcut.HOP
        out.append(loud if any(s <= t < e for s, e in spans) else quiet)
    return out


def words_in(spans, per_span):
    out = []
    for (s, e), texts in zip(spans, per_span):
        step = (e - s) / len(texts)
        for k, w in enumerate(texts):
            out.append({"w": w, "s": round(s + k * step, 3), "e": round(s + (k + 1) * step - 0.02, 3)})
    return out


def test_pauses_are_cut_with_breath_pads_and_words_kept():
    spans = [(0.5, 2.0), (3.5, 5.0), (5.2, 6.5)]
    words = words_in(spans, [["one", "two", "three"], ["four", "five"], ["six", "seven"]])
    plan = jumpcut.plan_from_levels(levels_for(spans, 8.0), words, 8.0, 30, SETTINGS)
    assert plan and plan["gate"] == "level"
    # the 1.5 s pause goes, the 0.2 s one stays, the head and tail silence go
    assert 2.0 < plan["removed"] < 4.0
    kept_words = jumpcut.remap_words(words, plan["keeps"])
    assert [w["w"] for w in kept_words] == [w["w"] for w in words]
    for s, e in plan["keeps"]:
        assert abs(s * 30 - round(s * 30)) < 1e-6 and abs(e * 30 - round(e * 30)) < 1e-6   # on the frame grid
    # pads: speech never starts within 50 ms of a cut
    starts = [k[0] for k in plan["keeps"]]
    assert all(min(abs(st - w["s"]) for w in words) >= 0.05 or st == 0 for st in starts)


def test_fillers_and_retakes_are_cut_last_take_wins():
    spans = [(0.2, 1.4), (1.6, 2.0), (2.2, 4.4), (4.8, 7.4)]
    words = words_in(spans, [["so", "here's", "the"], ["um"], ["here", "is", "the", "exact", "tool"],
                             ["here", "is", "the", "exact", "tool", "I", "used"]])
    plan = jumpcut.plan_from_levels(levels_for(spans, 8.0), words, 8.0, 30, {**SETTINGS, "max_cut_ratio": 0.9})
    reasons = "+".join(c["reason"] for c in plan["cuts"])
    assert "filler" in reasons and "retake" in reasons
    kept = [w["w"] for w in jumpcut.remap_words(words, plan["keeps"])]
    assert "um" not in kept
    assert kept[-7:] == ["here", "is", "the", "exact", "tool", "I", "used"]
    assert kept.count("exact") == 1


def test_word_gaps_are_used_when_music_hides_the_pauses():
    spans = [(0.0, 1.5), (3.0, 4.5)]
    words = words_in(spans, [["a", "b", "c"], ["d", "e", "f"]])
    flat = [-30.0] * 500  # constant background: no usable contrast
    plan = jumpcut.plan_from_levels(flat, words, 5.0, 30, SETTINGS)
    assert plan["gate"] == "words"
    assert 1.0 < plan["removed"] < 1.5


def test_cap_eases_off_instead_of_gutting_a_clip():
    spans = [(i * 1.0, i * 1.0 + 0.5) for i in range(10)]
    words = words_in(spans, [["again", "and", "again", "we", "go"]] * 10)   # every line a "retake"
    plan = jumpcut.plan_from_levels([-30.0] * 1000, words, 10.0, 30, {**SETTINGS, "max_cut_ratio": 0.2})
    assert plan is None or plan["removed"] <= 2.0


def test_nothing_to_cut_returns_none():
    spans = [(0.0, 5.0)]
    words = words_in(spans, [["just", "talking", "the", "whole", "time"]])
    assert jumpcut.plan_from_levels(levels_for(spans, 5.0), words, 5.0, 30, SETTINGS) is None


def test_settings_follow_recipe_then_config(cfg):
    assert jumpcut.settings_for(cfg, {})["pace"] == "tight"
    assert jumpcut.settings_for(cfg, {"jumpcut": "loose"})["pace"] == "loose"
    assert jumpcut.settings_for(cfg, {"jumpcut": "off"}) is None
    cfg.data["jumpcut"]["enabled"] = False
    assert jumpcut.settings_for(cfg, {}) is None


def test_gain_plan_fixed_gain_and_fallback():
    assert audio.gain_plan({"I": -20.0, "TP": -8.0})["gain_db"] == 6.0          # -14 reachable
    loud_peaks = audio.gain_plan({"I": -24.0, "TP": -2.0})                        # would limit 10.5 dB
    assert loud_peaks["target"] == -16.0 and loud_peaks["gain_db"] == 8.0
    assert audio.gain_plan({"I": float("-inf")})["gain_db"] == 0.0
    assert audio.parse_ebur128("Summary:\n  I:  -18.2 LUFS\n  LRA: 3.0 LU\n  Peak: -4.1 dBFS")["I"] == -18.2


def make_take(path):
    """6 s talking-head stand-in: tone bursts (speech) with a 1.5 s silent gap in the middle."""
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=15:d=6",
                    "-f", "lavfi", "-i", "sine=f=220:d=6,volume=0.3,"
                    "volume=enable='between(t,2,3.5)':volume=0",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def test_render_tightens_masters_and_keeps_sync(cfg, tmp_path):
    take = make_take(tmp_path / "take.mp4")
    words = [{"w": f"w{i}", "s": 0.2 + i * 0.4, "e": 0.5 + i * 0.4} for i in range(4)] + \
            [{"w": f"v{i}", "s": 3.7 + i * 0.4, "e": 4.0 + i * 0.4} for i in range(5)]
    clip = {"start": 0.0, "end": 6.0, "hook_overlay": "test"}
    out = render_clip(cfg, take, clip, words, default_recipe(cfg, clip), tmp_path / "out.mp4")
    rep = edit_report(out)
    assert rep["jumpcut"] and 1.0 < rep["jumpcut"]["removed"] < 2.0
    streams = json.loads(media.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration",
                                    "-of", "json", str(out)]).stdout)["streams"]
    durs = {s["codec_type"]: float(s["duration"]) for s in streams}
    assert abs(durs["video"] - durs["audio"]) < 0.07          # within one frame at 15 fps
    assert abs(durs["video"] - rep["duration_out"]) < 0.1
    assert abs(rep["audio"]["measured"]["I"] - rep["audio"]["target"]) <= 0.6
    assert rep["audio"]["measured"]["TP"] <= -1.0


def test_render_with_stop_motion_and_no_cuts(cfg, tmp_path):
    take = make_take(tmp_path / "take.mp4")
    clip = {"start": 0.0, "end": 6.0}
    recipe = {**default_recipe(cfg, clip), "jumpcut": "off", "stopmotion": {"look": "clay", "fps": 8}}
    out = render_clip(cfg, take, clip, [], recipe, tmp_path / "stop.mp4")
    assert edit_report(out)["jumpcut"] is None
    assert abs(media.probe(out)["duration"] - 6.0) < 0.1


def test_stop_motion_from_photos(tmp_path):
    photos = []
    for i, colour in enumerate(("red", "green", "blue")):
        p = tmp_path / f"frame{i + 1}.png"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s=320x240:d=1",
                        "-frames:v", "1", str(p)], check=True)
        photos.append(p)
    out = stopmotion.from_photos(photos, tmp_path / "stop.mp4", fps=12, hold=2, width=270, height=480, loop=2)
    info = media.probe(out)
    assert (info["width"], info["height"]) == (270, 480)
    assert abs(info["duration"] - 0.5) < 0.05       # 3 photos x 2 loops x 2 frames = 12 frames at 24 fps


def test_unticked_cut_is_kept_on_rerender():
    spans = [(0.5, 2.0), (3.5, 5.0), (6.5, 8.0)]
    words = words_in(spans, [["one", "two"], ["three", "four"], ["five", "six"]])
    first = jumpcut.plan_from_levels(levels_for(spans, 8.5), words, 8.5, 30, SETTINGS)
    inner = [c for c in first["cuts"] if 1.9 < c["start"] < 2.5]
    assert inner
    again = jumpcut.plan_from_levels(levels_for(spans, 8.5), words, 8.5, 30,
                                     {**SETTINGS, "keep": [round(inner[0]["start"], 2)]})
    assert again["removed"] < first["removed"] - 1.0
    assert not any(abs(c["start"] - inner[0]["start"]) < 0.2 for c in again["cuts"])


def test_render_writes_srt_on_the_cut_timeline(cfg, tmp_path):
    take = make_take(tmp_path / "take.mp4")
    words = [{"w": f"w{i}", "s": 0.2 + i * 0.4, "e": 0.5 + i * 0.4} for i in range(4)] + \
            [{"w": f"v{i}", "s": 3.7 + i * 0.4, "e": 4.0 + i * 0.4} for i in range(5)]
    out = render_clip(cfg, take, {"start": 0.0, "end": 6.0}, words, default_recipe(cfg, {}), tmp_path / "o.mp4")
    srt = out.with_suffix(".srt").read_text(encoding="utf-8")
    assert srt.startswith("1\n00:00:00,") and "v0" in srt
    last_end = srt.strip().splitlines()[-2].split(" --> ")[1]
    assert last_end < "00:00:05,000"          # timed to the tightened clip, not the 6 s source
