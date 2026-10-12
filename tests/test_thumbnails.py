import json
import shutil

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from kir_clipper import db
from kir_clipper.longform import set_segments, worker_tick
from kir_clipper.pipeline import process_file
from kir_clipper.thumbnails import ACCENTS, LAYOUTS, concepts, expressions, render
from kir_clipper.web.app import create_app

from .conftest import FakeLLM, FakeTranscriber


def person(folder, name="shocked"):
    folder.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (500, 800), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([150, 40, 350, 260], fill=(220, 170, 140, 255))
    d.rounded_rectangle([60, 260, 440, 800], 100, fill=(40, 90, 200, 255))
    img.save(folder / f"{name}.png")


def test_every_layout_and_accent_renders(cfg, tmp_path):
    person(tmp_path / "p")
    cfg.data["thumbnails"]["person_dir"] = str(tmp_path / "p")
    for i, layout in enumerate(LAYOUTS):
        for accent in ACCENTS:
            out = render(cfg, {"text": "STOP WAITING FOR MOTIVATION", "emphasis": "MOTIVATION", "layout": layout,
                               "expression": "shocked", "accent": accent}, tmp_path / f"{layout}_{accent}.jpg", seed=i)
            img = Image.open(out)
            assert img.size == (1280, 720) and out.stat().st_size < 2_000_000
    # Without a photo the words get the whole left side.
    cfg.data["thumbnails"]["person_dir"] = str(tmp_path / "none")
    render(cfg, {"text": "NOBODY IS COMING", "emphasis": "NOBODY", "layout": "split", "expression": "", "accent": "question"},
           tmp_path / "noperson.jpg")


def test_theme_colours_are_used(cfg, tmp_path):
    out = render(cfg, {"text": "HI", "emphasis": "", "layout": "versus", "expression": "", "accent": "none"},
                 tmp_path / "v.jpg")
    img = Image.open(out).convert("RGB")
    left, right = img.getpixel((40, 600)), img.getpixel((1240, 600))
    assert left[0] > left[2] and right[2] > right[0]          # red half and blue half


def test_concepts_are_trimmed_and_mapped_to_real_photos(cfg, tmp_path):
    person(tmp_path / "p", "serious")
    cfg.data["thumbnails"]["person_dir"] = str(tmp_path / "p")
    assert expressions(cfg) == ["serious"]

    def llm(system, user, schema):
        assert "serious" in user
        return {"concepts": [{"text": "ONE TWO THREE FOUR FIVE SIX", "emphasis": "TWO", "layout": "split",
                              "expression": "laughing", "accent": "arrow", "why": "x"}]}

    c = concepts(cfg, llm, {"title": "t"}, [], count=1)[0]
    assert c["text"] == "ONE TWO THREE FOUR" and c["expression"] == "serious"


def test_long_form_gets_thumbnails_you_can_pick_and_regenerate(cfg, conn, sample_video):
    cfg.data["longform"].update(width=640, height=360, fps=15)
    shutil.copy(sample_video, cfg.path("inbox") / "s.mp4")
    process_file(cfg, conn, cfg.path("inbox") / "s.mp4", llm=FakeLLM(scores=(70,)), transcriber=FakeTranscriber())
    st = conn.execute("SELECT * FROM streams").fetchone()
    set_segments(conn, st["id"], [{"start": 0, "end": st["duration"]}], st["duration"])
    worker_tick(cfg, conn, llm=FakeLLM())
    lf = conn.execute("SELECT * FROM longform").fetchone()
    plan = json.loads(lf["plan_json"])
    assert len(plan["thumbnails"]) == 3 and plan["thumbnail_choice"] == 0

    client = TestClient(create_app(cfg))
    assert client.get(f"/media/thumb/{lf['id']}/2").headers["content-type"] == "image/jpeg"
    r = client.post(f"/api/longform/{lf['id']}/thumbnail", json={"choice": 2})
    assert r.json()["choice"] == 2
    listing = client.get("/api/longform").json()
    assert listing[0]["segments"][0]["thumbnail_choice"] == 2
