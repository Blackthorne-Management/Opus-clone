"""Two people on stream: the clip is cut in half and stacked, guest on top, you underneath, captions between."""

import json
import subprocess

import pytest

from kir_clipper import media, render
from kir_clipper.render import build_filtergraph, default_recipe, render_clip, split_panels

LEFT = {"x": 0.22, "y": 0.3, "h": 0.15}
RIGHT = {"x": 0.78, "y": 0.32, "h": 0.15}


def test_guest_goes_on_top_and_swap_flips_it():
    top, bottom, side = split_panels([LEFT, RIGHT], me_side="left")
    assert (top, bottom, side) == (RIGHT, LEFT, "right")
    top, bottom, side = split_panels([LEFT, RIGHT], me_side="left", swap=True)
    assert (top, bottom, side) == (LEFT, RIGHT, "left")
    assert split_panels([LEFT, RIGHT], me_side="right")[0] == LEFT


@pytest.mark.parametrize("src", [(1920, 1080), (1280, 720), (2560, 1080)])
def test_panel_crops_stay_inside_the_frame(src):
    W, H = src
    for p in (LEFT, RIGHT, {"x": 0.01, "y": 0.99, "h": 0.1}):
        cw, ch, x, y = render._panel_crop(p, W, H, 1080 / 960)
        assert cw % 2 == 0 and ch % 2 == 0
        assert 0 <= x <= W - cw and 0 <= y <= H - ch
        assert cw <= W / 2 + 1 and abs(cw / ch - 1080 / 960) < 0.02


def test_filtergraph_stacks_two_panels():
    g = build_filtergraph({"width": 1920, "height": 1080}, {"crop_mode": "face", "zoom": "none"}, 1080, 1920, 30, None,
                          False, "captions.ass", None, split=(RIGHT, LEFT))
    assert "vstack=inputs=2" in g and g.count("crop=") == 2 and "scale=1080:960" in g


def _two_up(path):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=15",
                    "-f", "lavfi", "-i", "sine=f=220:d=4", "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


WORDS = [{"w": w, "s": 0.2 + i * 0.4, "e": 0.55 + i * 0.4, "p": 1.0} for i, w in enumerate("so what made you start".split())]


def test_render_stacks_when_two_people_are_found(cfg, tmp_path, monkeypatch):
    src = _two_up(tmp_path / "two.mp4")
    monkeypatch.setattr(render, "detect_people", lambda *a, **k: [LEFT, RIGHT])
    clip = {"start": 0.0, "end": 4.0, "hook": "x"}
    recipe = {**default_recipe(cfg, clip, use_style=False), "jumpcut": "off", "behind_word": True, "zoom": "punch"}
    out = render_clip(cfg, src, clip, WORDS, recipe, tmp_path / "o.mp4")
    rep = json.loads(out.with_suffix(".edit.json").read_text())
    assert rep["layout"] == {"stacked": True, "top": "right"}
    assert rep["behind"]["placed"] is False and "stacked" in rep["behind"]["reason"]
    info = media.probe(out)
    assert (info["width"], info["height"]) == (cfg.get("render.width"), cfg.get("render.height"))


def test_render_without_two_people_keeps_the_normal_crop(cfg, tmp_path, monkeypatch):
    src = _two_up(tmp_path / "one.mp4")
    monkeypatch.setattr(render, "detect_people", lambda *a, **k: None)
    clip = {"start": 0.0, "end": 4.0}
    out = render_clip(cfg, src, clip, WORDS, {**default_recipe(cfg, clip, use_style=False), "jumpcut": "off"},
                      tmp_path / "a.mp4")
    assert "layout" not in json.loads(out.with_suffix(".edit.json").read_text())
    out = render_clip(cfg, src, clip, WORDS, {**default_recipe(cfg, clip, use_style=False), "jumpcut": "off",
                                              "crop_mode": "split"}, tmp_path / "b.mp4")
    assert json.loads(out.with_suffix(".edit.json").read_text())["layout"]["stacked"] is False


def test_auto_stacking_can_be_turned_off(cfg, tmp_path, monkeypatch):
    src = _two_up(tmp_path / "two.mp4")
    calls = []
    monkeypatch.setattr(render, "detect_people", lambda *a, **k: calls.append(1) or [LEFT, RIGHT])
    cfg.data.setdefault("split", {})["auto"] = False
    clip = {"start": 0.0, "end": 4.0}
    render_clip(cfg, src, clip, WORDS, {**default_recipe(cfg, clip, use_style=False), "jumpcut": "off"}, tmp_path / "o.mp4")
    assert calls == []


def test_real_faces_are_found_side_by_side(tmp_path):
    cv2 = pytest.importorskip("cv2")
    skdata = pytest.importorskip("skimage.data")
    import numpy as np
    face = cv2.resize(skdata.astronaut()[:, :, ::-1], (800, 800))
    frame = np.full((1080, 1920, 3), 40, np.uint8)
    frame[140:940, 80:880] = face
    frame[140:940, 1040:1840] = cv2.flip(face, 1)
    cv2.imwrite(str(tmp_path / "f.png"), frame)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-loop", "1", "-i", str(tmp_path / "f.png"), "-t", "4", "-r", "10",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(tmp_path / "v.mp4")], check=True)
    people = render.detect_people(tmp_path / "v.mp4", 0, 4)
    assert people and people[0]["x"] < 0.4 < 0.6 < people[1]["x"]
