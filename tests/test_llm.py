import json

import anthropic
import httpx2 as httpx
import pytest

from kir_clipper.highlights import CANDIDATE_SCHEMA
from kir_clipper.llm import ClaudeJSON, LLMError


def sse(text: str, stop_reason: str = "end_turn") -> bytes:
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                           "usage": {"output_tokens": 5}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


def make(cfg, monkeypatch, body: bytes, seen: dict):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-000000000000")

    def handler(req: httpx.Request):
        seen["json"] = json.loads(req.content)
        seen["beta"] = req.headers.get("anthropic-beta")
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    llm = ClaudeJSON(cfg)
    llm.client = anthropic.Anthropic(http_client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=0)
    return llm


def test_request_shape_and_parse(cfg, monkeypatch):
    seen = {}
    llm = make(cfg, monkeypatch, sse('{"clips": []}'), seen)
    assert llm("sys", "user text", CANDIDATE_SCHEMA) == {"clips": []}
    body = seen["json"]
    assert body["model"] == cfg.get("llm.model")
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": CANDIDATE_SCHEMA}
    assert body["output_config"]["effort"] == cfg.get("llm.effort")
    assert body["thinking"] == {"type": "adaptive"}
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen["beta"]
    assert body["system"] == "sys" and body["stream"] is True


def test_refusal_raises(cfg, monkeypatch):
    llm = make(cfg, monkeypatch, sse("", stop_reason="refusal"), {})
    with pytest.raises(LLMError):
        llm("sys", "user", CANDIDATE_SCHEMA)


def test_missing_key_is_clear(cfg, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        ClaudeJSON(cfg)
