import base64
import io
import json
import logging

import httpx
import pytest
from PIL import Image, ImageStat

from kir_clipper import gemini_images
from kir_clipper.gemini_images import GeminiError, generate_image
from kir_clipper.logging_setup import redact
from kir_clipper.thumbnails import generate, render

from .conftest import FakeLLM
from .test_thumbnails import person

KEY = "AIzaSyTESTKEY000000000000000000000000000"


def png_b64(color=(0, 200, 0), size=(1376, 768)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def ok_response(color=(0, 200, 0)):
    return {"candidates": [{"content": {"parts": [{"text": "here you go"},
                                                    {"inlineData": {"mimeType": "image/png", "data": png_b64(color)}}]}}]}


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    gemini_images._working_model.clear()


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_request_shape(cfg, key, tmp_path):
    seen = {}

    def handler(req):
        seen["url"], seen["key"] = str(req.url), req.headers["x-goog-api-key"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=ok_response())

    ref = tmp_path / "ref.png"
    Image.new("RGBA", (50, 50), (255, 0, 0, 128)).save(ref)
    img = generate_image(cfg, "a prompt", refs=[ref], client=client_for(handler))
    assert img.size == (1376, 768)
    assert seen["url"].endswith("/v1beta/models/gemini-3.1-flash-image:generateContent") and seen["key"] == KEY
    gc = seen["body"]["generationConfig"]
    assert gc["responseModalities"] == ["IMAGE"] and gc["imageConfig"] == {"aspectRatio": "16:9", "imageSize": "1K"}
    parts = seen["body"]["contents"][0]["parts"]
    assert parts[0]["text"] == "a prompt" and parts[1]["inlineData"]["mimeType"] == "image/png"


def test_renamed_model_is_found_automatically(cfg, key):
    calls = []

    def handler(req):
        calls.append(str(req.url))
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"models": [{"name": "models/gemini-3.5-pro"},
                                                        {"name": "models/gemini-3-pro-image-preview"},
                                                        {"name": "models/gemini-3.2-flash-image-preview"}]})
        if "gemini-3.1-flash-image:" in req.url.path:
            return httpx.Response(404, json={"error": {"message": "not found"}})
        return httpx.Response(200, json=ok_response())

    generate_image(cfg, "p", client=client_for(handler))
    assert "gemini-3.2-flash-image-preview:generateContent" in calls[-1]
    calls.clear()
    generate_image(cfg, "p", client=client_for(handler))          # remembered for the session
    assert len(calls) == 1 and "gemini-3.2-flash-image-preview" in calls[0]


def test_errors_are_clear(cfg, key, monkeypatch):
    with pytest.raises(GeminiError, match="API key"):
        generate_image(cfg, "p", client=client_for(lambda r: httpx.Response(403, json={})))
    blocked = {"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []}
    with pytest.raises(GeminiError, match="SAFETY"):
        generate_image(cfg, "p", client=client_for(lambda r: httpx.Response(200, json=blocked)))
    monkeypatch.delenv("GEMINI_API_KEY")
    with pytest.raises(GeminiError, match="GEMINI_API_KEY"):
        generate_image(cfg, "p")


def test_ai_background_keeps_the_channel_look(cfg, key, tmp_path):
    prompts = []

    def handler(req):
        prompts.append(json.loads(req.content)["contents"][0]["parts"][0]["text"])
        return httpx.Response(200, json=ok_response((0, 220, 0)))   # bright green art

    made = generate(cfg, FakeLLM(), {"title": "Why discipline wins"}, [], tmp_path, "t", count=2,
                    gemini_client=client_for(handler))
    assert [m["source"] for m in made] == ["gemini-background"] * 2
    assert "no text" in prompts[0].lower() and "Why discipline wins" in prompts[0]
    img = Image.open(made[0]["path"]).convert("RGB")
    assert img.size == (1280, 720)
    plain = Image.open(render(cfg, made[0], tmp_path / "plain.jpg", seed=hash(("t", 0)) & 0xFFFF)).convert("RGB")
    box = (700, 80, 1100, 300)                     # background area, clear of the words and the accent
    mean = lambda im, ch: ImageStat.Stat(im.crop(box)).mean[ch]
    assert mean(img, 1) > mean(plain, 1) + 20      # the (green) AI art shows through...
    assert mean(img, 0) + mean(img, 2) > 20        # ...still tinted with the channel red/blue (the art had none)


def test_full_mode_uses_photos_and_past_picks(cfg, key, tmp_path):
    person(tmp_path / "p", "shocked")
    cfg.data["thumbnails"]["person_dir"] = str(tmp_path / "p")
    cfg.data["thumbnails"]["ai"]["mode"] = "full"
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=ok_response())

    llm = FakeLLM()
    made = generate(cfg, llm, {"title": "T"}, [], tmp_path, "f", count=1, gemini_client=client_for(handler))
    assert made[0]["source"] == "gemini-full"
    parts = seen[0]["contents"][0]["parts"]
    assert '"STOP WAITING"' in parts[0]["text"] and len(parts) == 2         # prompt + the person photo
    assert Image.open(made[0]["path"]).size == (1280, 720)


def test_falls_back_to_template(cfg, tmp_path, caplog):
    made = generate(cfg, FakeLLM(), {"title": "T"}, [], tmp_path, "x", count=1)   # no key in tests
    assert made[0]["source"] == "template" and "GEMINI_API_KEY" in made[0]["ai_error"]
    assert Image.open(made[0]["path"]).size == (1280, 720)


def test_google_keys_are_scrubbed_from_logs():
    assert KEY not in redact(f"calling with key={KEY}")
