"""My style (learned + rules), notes on clips, the word behind your head, and sound effects."""

import json
import subprocess
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from kir_clipper import db, depth, notes, sfx, style
from kir_clipper.captions import build_ass
from kir_clipper.render import default_recipe, edit_report, render_clip
from kir_clipper.web.app import create_app

from .conftest import FakeTranscriber, make_video

BG = (60, 80, 100)


@pytest.fixture
def scfg(cfg, tmp_path):
    cfg.data.setdefault("style", {})["file"] = str(tmp_path / "my-style.json")
    return cfg


# --------------------------------------------------------------------------- style

def test_style_values_are_checked():
    assert style.coerce("caption_size", "2.5") == 1.6                      # clamped
    assert style.coerce("highlight_color", "#ff00aa") == "#FF00AA"
    with pytest.raises(ValueError):
        style.coerce("caption_style", "comic sans")
    with pytest.raises(ValueError):
        style.coerce("primary_color", "red")
    assert style.clean({"caption_size": 1.2, "nonsense": 1, "zoom": "spin"}) == {"caption_size": 1.2}


def test_style_and_rules_flow_into_new_clips(scfg):
    assert default_recipe(scfg, {})["caption_size"] == 1.0
    style.set_settings(scfg, {"caption_style": "boxed", "caption_size": 1.2, "sfx": "medium"})
    style.add_rule(scfg, "Captions sit higher", {"caption_y": 0.6, "made_up": 3})
    r = default_recipe(scfg, {})
    assert (r["caption_style"], r["caption_size"], r["sfx"], r["caption_y"]) == ("boxed", 1.2, "medium", 0.6)
    assert default_recipe(scfg, {}, use_style=False)["caption_style"] == "hormozi"
    rule_id = style.load(scfg)["rules"][0]["id"]
    assert style.remove_rule(scfg, rule_id) and default_recipe(scfg, {})["caption_y"] is None
    with pytest.raises(ValueError):
        style.add_rule(scfg, "nothing", {"start": 3})


def test_suggestions_from_changes_you_keep_making(scfg, conn):
    for _ in range(3):
        db.log_event(conn, "clip_rerendered", 1, {"recipe": {"caption_style": "boxed"}})
    db.log_event(conn, "clip_rerendered", 1, {"recipe": {"zoom": "none"}})
    sug = style.suggestions(scfg, conn)
    assert [(s["knob"], s["value"], s["count"]) for s in sug] == [("caption_style", "boxed", 3)]
    style.set_settings(scfg, {"caption_style": "boxed"})
    assert style.suggestions(scfg, conn) == []                          # already your default


def test_caption_settings_reach_the_subtitles():
    words = [{"w": "hello", "s": 0.1, "e": 0.4}, {"w": "there", "s": 0.5, "e": 0.8}]
    base = build_ass(words, 2.0, {"caption_style": "bold_pop"}, {"font": "Montserrat Black"}, 1080, 1920)
    tuned = build_ass(words, 2.0, {"caption_style": "bold_pop", "caption_size": 1.5, "caption_y": 0.5,
                                   "caption_font": "Anton", "primary_color": "#112233"}, {"font": "Montserrat Black"}, 1080, 1920)
    cap = lambda ass: next(l for l in ass.splitlines() if l.startswith("Style: Cap,")).split(",")
    assert int(cap(base)[2]) == 96 and int(cap(tuned)[2]) == 144
    assert cap(tuned)[1] == "Anton" and cap(tuned)[3] == "&H00332211"
    assert int(cap(tuned)[21]) == 960                                   # bottom of the captions halfway down


# --------------------------------------------------------------------------- sound effects

def test_sfx_events_by_level():
    words = [{"w": "I", "s": 0.2, "e": 0.3}, {"w": "made", "s": 1.0, "e": 1.2}, {"w": "$400", "s": 2.0, "e": 2.4},
             {"w": "money", "s": 2.6, "e": 2.9}, {"w": "today", "s": 4.5, "e": 4.8}]
    recipe = {"zoom": "punch", "zoom_period": 3.0}
    assert sfx.events({**recipe, "sfx": "off"}, words, 8, 30, 0.05) == []
    assert sfx.events({**recipe, "sfx": "light"}, words, 8, 30, 0.05) == [("pop", 0.05)]
    medium = sfx.events({**recipe, "sfx": "medium"}, words, 8, 30, 0.05)
    assert [k for k, _ in medium].count("whoosh") == 2                  # punch-ins at 1.5 and 4.5 s (7.5 is the last half-second)
    full = sfx.events({**recipe, "sfx": "full"}, words, 8, 30, 0.05)
    assert [t for k, t in full if k == "tick"] == [2.0]                 # "money" is too close to "$400"


def test_sfx_track_and_own_sounds(cfg, tmp_path):
    folder = tmp_path / "sfx"
    folder.mkdir()
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=f=1000:d=0.2",
                    str(folder / "pop.wav")], check=True)
    cfg.data.setdefault("sfx", {})["folder"] = str(folder)
    own = sfx.sound(cfg, "pop")
    assert abs(own.size / sfx.SR - 0.2) < 0.01                          # yours, not the generated one
    out = sfx.track(cfg, [("pop", 0.5), ("tick", 1.0)], 2.0, tmp_path / "t.wav")
    with wave.open(str(out)) as wf:
        assert wf.getframerate() == 48000 and abs(wf.getnframes() / 48000 - 2.0) < 0.01
    assert sfx.track(cfg, [], 2.0, tmp_path / "none.wav") is None


# --------------------------------------------------------------------------- word behind your head

def person_video(path, seconds=4, w=270, h=480, fps=15):
    from PIL import Image, ImageDraw
    p = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
                          "-r", str(fps), "-i", "-", "-f", "lavfi", "-i", f"sine=f=220:d={seconds},volume=0.3",
                          "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                          str(path)], stdin=subprocess.PIPE)
    for _ in range(fps * seconds):
        img = Image.new("RGB", (w, h), BG)
        d = ImageDraw.Draw(img)
        d.ellipse((95, 165, 175, 255), fill=(220, 180, 150))
        d.rectangle((75, 250, 195, h), fill=(40, 40, 120))
        p.stdin.write(img.tobytes())
    p.stdin.close()
    p.wait()
    return path


def fake_mask(frame):
    """Anything that isn't the plain background is 'the person' (stands in for the cut-out model)."""
    diff = np.abs(frame.astype(int) - np.array(BG)).sum(axis=2)
    return (diff > 60).astype(np.float32)


def test_pick_word():
    words = [{"w": "So", "s": 0.1, "e": 0.2}, {"w": "honestly", "s": 0.4, "e": 0.8}, {"w": "$10,000", "s": 1.0, "e": 1.5},
             {"w": "money", "s": 5.0, "e": 5.3}]
    assert depth.pick_word(words) == {"text": "$10000", "s": 1.0}
    assert depth.pick_word(words, "Money!") == {"text": "MONEY", "s": 5.0}
    assert depth.pick_word(words, "banana") is None
    assert depth.pick_word([{"w": "so", "s": 0.1, "e": 0.2}]) is None


def test_head_and_placement():
    mask = np.zeros((480, 270), np.float32)
    mask[165:480, 95:175] = 1
    assert depth.head_top(mask) == 165
    mask[40:60, 0:20] = 1                                                 # a raised hand at the edge doesn't count
    assert depth.head_top(mask) == 165
    word = np.zeros((60, 230, 4), np.uint8)
    word[..., 3] = 255
    x, y = depth.place(word, mask, 165)
    assert y < 165 < y + 60 and x == 20


def test_word_goes_behind_the_person(cfg, tmp_path):
    video = person_video(tmp_path / "p.mp4")
    rep = depth.render(cfg, video, tmp_path / "out.mp4", {"text": "MONEY", "s": 1.0}, 15, 4.0, mask_fn=fake_mask)
    assert rep["placed"], rep
    frame = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", "2.0", "-i", str(tmp_path / "out.mp4"),
                            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    img = np.frombuffer(frame, np.uint8).reshape(480, 270, 3).astype(int)
    above_head = img[120:160, 20:250]                                     # the word, in the background above the head
    assert (above_head.sum(axis=2) > 600).mean() > 0.05
    face = img[200:240, 115:155]                                          # the face stays in front of the word
    assert np.abs(face - [220, 180, 150]).sum(axis=2).mean() < 60
    before = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", "0.3", "-i", str(tmp_path / "out.mp4"),
                             "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    assert (np.frombuffer(before, np.uint8).reshape(480, 270, 3)[120:160].astype(int).sum(axis=2) > 600).mean() < 0.01


def test_no_room_above_the_head_is_reported(cfg, tmp_path):
    video = person_video(tmp_path / "p.mp4")
    rep = depth.render(cfg, video, tmp_path / "o.mp4", {"text": "X", "s": 0.5}, 15, 4.0,
                       mask_fn=lambda f: np.ones(f.shape[:2], np.float32))
    assert not rep["placed"] and "space above" in rep["reason"]


def test_render_with_behind_word_and_sfx(cfg, tmp_path, monkeypatch):
    video = person_video(tmp_path / "p.mp4")
    real = depth.render
    monkeypatch.setattr(depth, "render", lambda *a, **k: real(*a, **{**k, "mask_fn": fake_mask}))
    words = [{"w": "I", "s": 0.2, "e": 0.4}, {"w": "made", "s": 0.5, "e": 0.8}, {"w": "money", "s": 0.9, "e": 1.3}]
    recipe = {**default_recipe(cfg, {"hook_overlay": "big hook"}), "behind_word": True, "sfx": "light",
              "jumpcut": "off", "crop_mode": "center", "zoom": "none"}
    out = render_clip(cfg, video, {"start": 0, "end": 4}, words, recipe, tmp_path / "out.mp4")
    rep = edit_report(out)
    assert rep["behind"]["placed"] and rep["behind"]["word"] == "MONEY"
    assert rep["sfx"] == {"level": "light", "count": 1}
    assert abs(rep["audio"]["measured"]["I"] - rep["audio"]["target"]) <= 0.6


# --------------------------------------------------------------------------- learn + notes

class VisionLLM:
    def __init__(self):
        self.content = None

    def __call__(self, system, user, schema, max_tokens=16000):
        self.content = user
        return {"summary": "Big boxed captions, fast cuts.", "observations": ["captions sit 62% down"],
                "settings": {"caption_style": "boxed", "caption_font": "Anton", "caption_size": 1.3, "caption_y": 0.62,
                             "caption_words": 2, "primary_color": "#FFFFFF", "highlight_color": "#00FF88",
                             "emphasis_color": "not a colour", "zoom": "punch", "zoom_period": 3.0, "jumpcut": "tight",
                             "hook_overlay": True, "behind_word": False, "sfx": "medium"},
                "cannot_copy": ["emoji stickers"]}


def test_learn_style_from_videos(scfg, tmp_path):
    a = make_video(tmp_path / "ref1.mp4", seconds=4)
    b = make_video(tmp_path / "ref2.mp4", seconds=4, color="testsrc")
    llm = VisionLLM()
    style.start_learning(scfg, [a, b], [], llm, background=False)
    prop = style.read_proposal(scfg)
    assert prop["status"] == "ready", prop
    images = [c for c in llm.content if c["type"] == "image"]
    assert len(images) == 14 and images[0]["source"]["media_type"] == "image/jpeg"
    assert "median shot" in llm.content[0]["text"]
    s = prop["proposal"]["settings"]
    assert s["caption_style"] == "boxed" and "emphasis_color" not in s       # a bad colour is dropped, not saved
    assert not a.exists()                                                  # uploads are cleaned up
    style.accept_proposal(scfg)
    assert default_recipe(scfg, {})["caption_words"] == 2
    assert style.load(scfg)["learned"]["summary"].startswith("Big boxed")
    with pytest.raises(ValueError):
        style.accept_proposal(scfg)


class NoteLLM:
    def __init__(self, general=True):
        self.general = general
        self.prompt = None

    def __call__(self, system, user, schema, max_tokens=16000):
        self.prompt = user
        return {"reply": "Making the captions bigger and starting a second later.",
                "changes": [{"setting": "caption_size", "value": "1.3"}, {"setting": "zoom", "value": "spin"}],
                "start_shift": 1.0, "end_shift": 0, "hook_text": "", "behind_text": "money",
                "general": self.general, "rule_text": "Bigger captions", "cannot_do": ""}


def test_note_rerenders_and_can_become_a_rule(scfg, conn, sample_video):
    from kir_clipper.pipeline import process_file
    from .conftest import FakeLLM
    inbox = scfg.path("inbox")
    import shutil
    src = inbox / "s.mp4"
    shutil.copy(sample_video, src)
    process_file(scfg, conn, src, llm=FakeLLM(), transcriber=FakeTranscriber())
    clip = db.clip_dict(conn.execute("SELECT * FROM clips WHERE file_path IS NOT NULL LIMIT 1").fetchone())
    llm = NoteLLM()
    res = notes.apply_note(scfg, conn, clip["id"], "captions bigger, start later, money behind my head", llm)
    assert res["rendered"] and res["offer_rule"]
    assert res["changes"] == {"caption_size": 1.3, "behind_word": True, "behind_text": "money"}   # "spin" dropped
    after = db.clip_dict(db.get_clip(conn, clip["id"]))
    assert after["recipe"]["caption_size"] == 1.3 and abs(after["start"] - (clip["start"] + 1.0)) < 0.01
    assert "caption_size" in llm.prompt and "word1" in llm.prompt
    rule = notes.save_rule(scfg, conn, res["id"])
    assert rule["changes"] == {"caption_size": 1.3, "behind_word": True}     # clip-only bits aren't in the rule
    assert default_recipe(scfg, {})["caption_size"] == 1.3
    with pytest.raises(ValueError):
        notes.apply_note(scfg, conn, clip["id"], "  ", llm)


def test_style_endpoints(scfg, conn, tmp_path):
    scfg.root = tmp_path
    client = TestClient(create_app(scfg))
    d = client.get("/api/style").json()
    assert "caption_size" in d["knobs"] and d["proposal"] == {} and d["claude"] is False
    assert client.post("/api/style", json={"settings": {"caption_style": "minimal"}}).status_code == 200
    assert client.get("/api/style").json()["settings"]["caption_style"] == "minimal"
    assert client.post("/api/style", json={"settings": {"caption_style": "wavy"}}).status_code == 400
    assert client.post("/api/style/learn", data={"links": "https://youtube.com/shorts/x"}).status_code == 400  # no key
    assert client.post("/api/clips/1/note", json={"note": "bigger"}).status_code == 400
    assert "My style" in client.get("/").text
