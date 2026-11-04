"""Animate an ad: brief, storyboard rules, the HyperFrames page, sound, the job and the dashboard routes.

The HyperFrames render itself is stood in for by ffmpeg unless KIR_TEST_HYPERFRAMES=1 (CI sets it on Linux,
where Node 22 is installed), in which case one real 15-second ad is rendered with `npx hyperframes`.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kir_clipper import animate, db, media
from kir_clipper.web.app import create_app

BRIEF = {"product": "The Shadow Routine", "cta": "Join the next cohort", "handle": "theshadowroutine.app",
         "length": 30, "aspect": "9:16", "variations": 2, "proof": "312 members this cycle"}
PAL = {"ground": "#000", "ground2": "#111", "ink": "#fff", "muted": "#aaa", "accent": "#FFD400", "on_accent": "#111"}


def scene(kind, headline, seconds=3.0, **kw):
    return {"kind": kind, "headline": headline, "emphasis": kw.get("emphasis", ""), "sub": kw.get("sub", ""),
            "items": kw.get("items", []), "image": kw.get("image", -1), "seconds": seconds}


STORYBOARD = {"variations": [
    {"angle": "pain call-out", "scenes": [
        scene("hook", "You skipped the gym again.", 2, emphasis="again"),
        scene("problem", "Motivation runs out by Wednesday every single week", 4, emphasis="Thursday"),
        scene("reveal", "The Shadow Routine", 4, image=0, sub="A cohort that keeps you honest"),
        scene("benefits", "What you get", 5, items=["Daily missions", "A cohort watching", "Ranks", "A fourth"]),
        scene("proof", "312 members this cycle", 3, emphasis="members"),
        scene("cta", "Join the next cohort", 3, sub="theshadowroutine.app")]},
    {"angle": "question", "scenes": [
        scene("problem", "Why do you quit by week two?", 3, image=7),
        scene("benefits", "Fix it", 4, items=["Missions"])]}],
    "missing": ["a member's before/after", " "]}


class FakeLLM:
    def __init__(self, out=STORYBOARD):
        self.out, self.calls = out, []

    def __call__(self, system, user, schema):
        self.calls.append(user)
        return self.out


def test_clean_brief_rules():
    b = animate.clean_brief({**BRIEF, "length": 33, "aspect": "2:1", "variations": 9, "sfx": "loud"})
    assert (b["length"], b["aspect"], b["variations"], b["sfx"], b["ground"]) == (30, "9:16", 5, "light", "dark")
    with pytest.raises(ValueError):
        animate.clean_brief({"cta": "Buy"})
    with pytest.raises(ValueError):
        animate.clean_brief({"product": "X"})


def test_normalise_keeps_the_rules_and_the_length():
    brief = animate.clean_brief(BRIEF)
    out = animate.normalise(STORYBOARD, brief, n_images=1)
    assert len(out) == 2
    a = out[0]["scenes"]
    assert a[0]["kind"] == "hook" and a[-1]["kind"] == "cta"
    assert a[0]["headline"] == "You skipped the gym again"               # no ending period
    assert len(a[1]["headline"].split()) <= animate.MAX_WORDS
    assert a[1]["emphasis"] == ""                                         # not a word on screen: dropped
    assert len(a[3]["items"]) == 3
    assert a[0]["start"] == 0 and abs(a[-1]["start"] + a[-1]["seconds"] - 30) < 0.01
    assert all(abs(x["start"] + x["seconds"] - y["start"]) < 0.02 for x, y in zip(a, a[1:]))
    b = out[1]["scenes"]
    assert b[0]["kind"] == "hook" and b[0]["image"] == -1                 # image 7 doesn't exist
    assert b[-1]["kind"] == "cta" and b[-1]["headline"] == "Join the next cohort"   # added from the brief


def test_prompt_only_uses_the_brief():
    system, user = animate._prompt(animate.clean_brief(BRIEF), ["leaderboard screen"])
    assert "never invent" in system.lower()
    assert "0: leaderboard screen" in user and "312 members" in user


def test_html_follows_the_hyperframes_contract():
    brief = animate.clean_brief(BRIEF)
    scenes = animate.normalise(STORYBOARD, brief, 1)[0]["scenes"]
    page = animate.build_html(scenes, (1080, 1920), PAL, ["img0.png"], 30)
    assert 'data-composition-id="main"' in page and 'data-duration="30"' in page
    assert 'window.__timelines["main"] = tl' in page and "paused: true" in page
    for bad in ("Math.random", "Date.now", "repeat: -1", "setTimeout"):
        assert bad not in page
    clips = re.findall(r'<div[^>]*class="[^"]*\bclip\b[^"]*"[^>]*>', page)
    assert len(clips) == len(scenes) + 1                                  # every scene + the background glow
    assert all("data-start=" in c and "data-duration=" in c for c in clips)
    ids = re.findall(r'id="([^"]+)"', page)
    assert len(ids) == len(set(ids))
    assert 'src="assets/img0.png"' in page and "assets/fonts/Anton-Regular.ttf" in page
    assert 'class="count"' in page                                        # the proof number counts up
    assert "top: 168px" in page                                           # words stay out of the apps' UI


def test_html_escapes_text():
    s = [{**scene("hook", '<b>"Free"</b> & more', 3), "start": 0}, {**scene("cta", "Go", 2), "start": 3}]
    page = animate.build_html(s, (1080, 1080), PAL, [], 5)
    assert "<b>" not in page.split("<body>")[1] and "&amp;" in page


def test_sfx_events():
    scenes = [{**scene("hook", "a"), "start": 0}, {**scene("benefits", "b", items=["x", "y"]), "start": 3},
              {**scene("cta", "c"), "start": 6}]
    assert animate.sfx_events(scenes, "off") == []
    light = animate.sfx_events(scenes, "light")
    assert [k for k, _ in light] == ["whoosh", "whoosh", "pop"]
    assert len(animate.sfx_events(scenes, "full")) == 5


def _silent_video(path: Path, seconds: float, size=(216, 384)):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c=black:s={size[0]}x{size[1]}:r=30",
                    "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)


def test_add_sound_masters_music_and_effects(cfg, tmp_path):
    silent = tmp_path / "silent.mp4"
    _silent_video(silent, 6)
    music = tmp_path / "m.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=f=220:d=2", str(music)], check=True)
    scenes = [{**scene("hook", "a"), "start": 0}, {**scene("cta", "c"), "start": 3}]
    out = tmp_path / "ad.mp4"
    rep = animate.add_sound(cfg, silent, out, scenes, 6.0, music, "light")
    assert rep == {"music": True, "sfx": 2}
    info = media.probe(out)
    assert abs(info["duration"] - 6) < 0.15
    # no music, effects off: the picture is copied as is
    rep = animate.add_sound(cfg, silent, tmp_path / "quiet.mp4", scenes, 6.0, None, "off")
    assert rep == {"music": False, "sfx": 0}


def _fake_hyperframes(monkeypatch, calls):
    def fake(args, cwd, timeout=900):
        calls.append(args[0])
        assert (Path(cwd) / "index.html").exists() and (Path(cwd) / "assets" / "fonts" / "Anton-Regular.ttf").exists()
        if args[0] == "render":
            _silent_video(Path(args[args.index("--output") + 1]), 15)
    monkeypatch.setattr(animate, "hyperframes", fake)
    monkeypatch.setattr(animate, "ready", lambda: (True, ""))


def test_create_storyboards_and_renders_every_version(cfg, conn, tmp_path, monkeypatch):
    calls = []
    _fake_hyperframes(monkeypatch, calls)
    img = tmp_path / "shot.png"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=90x160", "-frames:v", "1",
                    str(img)], check=True)
    llm = FakeLLM()
    batch = animate.create(cfg, conn, {**BRIEF, "length": 15}, [("leaderboard screen", img)], None, llm=llm,
                           background=False)
    ads = animate.list_ads(conn)
    assert len(ads) == 2 and {a["batch"] for a in ads} == {batch}
    assert all(a["status"] == "ready" and a["has_video"] for a in ads), [a["error"] for a in ads]
    assert calls == ["lint", "render", "lint", "render"]
    assert "0: leaderboard screen" in llm.calls[0]
    first = next(a for a in ads if a["variant"] == 1)
    assert first["report"]["missing"] == ["a member's before/after"]
    assert (Path(first["project_path"]) / "assets" / "img0.png").exists()
    assert not img.exists()                                               # moved into the ad's folder

    # change the words and render again: same timings
    old = first["scenes"]
    animate.rerender(cfg, conn, first["id"], [{"headline": "New hook words here", "sub": ""}] + [{}] * (len(old) - 1),
                     background=False)
    again = animate.ad_dict(conn.execute("SELECT * FROM ads WHERE id=?", (first["id"],)).fetchone())
    assert again["scenes"][0]["headline"] == "New hook words here" and again["status"] == "ready"
    assert [s["start"] for s in again["scenes"]] == [s["start"] for s in old]
    with pytest.raises(ValueError):
        animate.rerender(cfg, conn, first["id"], [{}], background=False)


def test_render_failure_is_shown(cfg, conn, monkeypatch):
    monkeypatch.setattr(animate, "ready", lambda: (True, ""))

    def broken(args, cwd, timeout=900):
        raise RuntimeError("HyperFrames lint failed: missing data-start")
    monkeypatch.setattr(animate, "hyperframes", broken)
    animate.create(cfg, conn, BRIEF, [], None, llm=FakeLLM(), background=False)
    ads = animate.list_ads(conn)
    assert ads and all(a["status"] == "failed" and "lint failed" in a["error"] for a in ads)


def test_resume_marks_interrupted_renders(cfg, conn):
    now = db.iso(db.utcnow())
    conn.execute("INSERT INTO ads(batch, variant, name, brief, status, created_at, updated_at) VALUES (1,1,'x','{}','rendering',?,?)",
                 (now, now))
    animate.resume(cfg, conn)
    assert conn.execute("SELECT status FROM ads").fetchone()["status"] == "failed"


def test_not_ready_without_node(monkeypatch):
    monkeypatch.setattr(animate, "node_version", lambda: None)
    assert animate.ready()[0] is False
    monkeypatch.setattr(animate, "node_version", lambda: 20)
    ok, why = animate.ready()
    assert not ok and "too old" in why


def test_dashboard_routes(cfg, conn, monkeypatch, tmp_path):
    calls = []
    _fake_hyperframes(monkeypatch, calls)
    import kir_clipper.llm as llm_mod
    monkeypatch.setattr(llm_mod, "ClaudeJSON", lambda cfg: FakeLLM())
    started = []
    real_create = animate.create
    monkeypatch.setattr(animate, "create", lambda *a, **k: started.append(1) or real_create(*a, **{**k, "background": False}))
    client = TestClient(create_app(cfg))
    d = client.get("/api/ads").json()
    assert d["ready"] and d["ads"] == [] and "9:16" in d["aspects"]
    r = client.post("/api/ads", data={"brief": json.dumps({**BRIEF, "length": 15, "variations": 1})})
    assert r.status_code == 200, r.text
    ad = client.get("/api/ads").json()["ads"][0]
    assert ad["status"] == "ready"
    v = client.get(f"/media/ad/{ad['id']}?download=1")
    assert v.status_code == 200 and "attachment" in v.headers.get("content-disposition", "")
    assert client.post("/api/ads", data={"brief": json.dumps({"product": ""})}).status_code == 400
    assert client.get("/media/ad/999").status_code == 404


@pytest.mark.skipif(os.environ.get("KIR_TEST_HYPERFRAMES") != "1" or not animate.ready()[0],
                    reason="real HyperFrames render: set KIR_TEST_HYPERFRAMES=1 with Node 22+")
def test_real_hyperframes_render(cfg, conn, tmp_path):
    img = tmp_path / "shot.png"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=450x800", "-frames:v", "1",
                    str(img)], check=True)
    animate.create(cfg, conn, {**BRIEF, "length": 15, "variations": 1}, [("app screen", img)], None, llm=FakeLLM(),
                   background=False)
    ad = animate.list_ads(conn)[0]
    assert ad["status"] == "ready", ad["error"]
    info = media.probe(Path(ad["video_path"]))
    assert (info["width"], info["height"]) == (1080, 1920) and abs(info["duration"] - 15) < 0.2
