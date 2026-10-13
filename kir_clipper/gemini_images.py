"""Gemini image generation (Nano Banana family) for thumbnail art.

REST: POST {base}/v1beta/models/{model}:generateContent with header x-goog-api-key.
Body: contents[].parts[] = text + optional inlineData reference images;
generationConfig.responseModalities = ["IMAGE"], generationConfig.imageConfig = {aspectRatio, imageSize}.
The image comes back as base64 in candidates[0].content.parts[].inlineData.data.

If the configured model name is rejected (models get renamed and retired), the app asks the API which image
models exist and uses the best one, remembering it for the rest of the session.
"""

from __future__ import annotations

import base64
import io
import logging
from pathlib import Path

import httpx
from PIL import Image

from .config import Config, secret

log = logging.getLogger("kir.gemini")

BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.1-flash-image"
_working_model: dict[str, str] = {}


class GeminiError(RuntimeError):
    pass


def api_key() -> str | None:
    return secret("GEMINI_API_KEY") or secret("GOOGLE_API_KEY")


def _encode(path: Path, max_side: int = 1024) -> dict:
    img = Image.open(path)
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    if has_alpha:
        img.convert("RGBA").save(buf, "PNG")
        mime = "image/png"
    else:
        img.convert("RGB").save(buf, "JPEG", quality=90)
        mime = "image/jpeg"
    return {"inlineData": {"mimeType": mime, "data": base64.b64encode(buf.getvalue()).decode()}}


def _pick_image_model(client: httpx.Client, key: str) -> str | None:
    """Ask the API which models can make images; prefer flash (cheaper), then pro."""
    try:
        resp = client.get(f"{BASE}/models", headers={"x-goog-api-key": key}, params={"pageSize": 200})
        names = [m.get("name", "").removeprefix("models/") for m in resp.json().get("models", [])]
    except (httpx.HTTPError, ValueError):
        return None
    image_models = [n for n in names if "image" in n and n.startswith("gemini")]
    for pref in ("flash-image", "pro-image", "image"):
        match = sorted((n for n in image_models if pref in n), reverse=True)
        if match:
            return match[0]
    return None


def generate_image(cfg: Config, prompt: str, refs: list[Path] | None = None, client: httpx.Client | None = None,
                   aspect: str = "16:9") -> Image.Image:
    key = api_key()
    if not key:
        raise GeminiError("GEMINI_API_KEY is not set (add it to .env)")
    client = client or httpx.Client(timeout=httpx.Timeout(180, connect=20))
    ai = cfg.get("thumbnails.ai") or {}
    configured = ai.get("model") or DEFAULT_MODEL
    model = _working_model.get(configured, configured)
    parts = [{"text": prompt}] + [_encode(Path(r)) for r in (refs or []) if Path(r).exists()]
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"],
                                 "imageConfig": {"aspectRatio": aspect, "imageSize": ai.get("image_size", "1K")}}}
    for attempt in range(2):
        try:
            resp = client.post(f"{BASE}/models/{model}:generateContent", headers={"x-goog-api-key": key}, json=body)
        except httpx.HTTPError as exc:
            raise GeminiError(f"network error talking to Gemini: {type(exc).__name__}") from exc
        if resp.status_code == 404 and attempt == 0:
            found = _pick_image_model(client, key)
            if found and found != model:
                log.warning("Gemini model %s not available; using %s instead", model, found)
                _working_model[configured] = model = found
                continue
        if resp.status_code in (401, 403):
            raise GeminiError("Gemini rejected the API key (check GEMINI_API_KEY and that billing is enabled)")
        if resp.status_code >= 300:
            try:
                msg = resp.json().get("error", {}).get("message", "")
            except ValueError:
                msg = resp.text[:200]
            raise GeminiError(f"Gemini error {resp.status_code}: {msg[:300]}")
        data = resp.json()
        for cand in data.get("candidates", []):
            for part in (cand.get("content") or {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    return Image.open(io.BytesIO(base64.b64decode(inline["data"]))).convert("RGBA")
        reason = (data.get("promptFeedback") or {}).get("blockReason") or \
            next((c.get("finishReason") for c in data.get("candidates", []) if c.get("finishReason")), "no image returned")
        raise GeminiError(f"Gemini returned no image ({reason})")
    raise GeminiError("Gemini model not available")
