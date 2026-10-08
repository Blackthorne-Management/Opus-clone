"""Claude API wrapper returning schema-validated JSON."""

from __future__ import annotations

import json
import logging
import time

import anthropic

from .config import Config, secret

log = logging.getLogger("kir.llm")


class LLMError(RuntimeError):
    pass


class ClaudeJSON:
    """Calls Claude with structured outputs (`output_config.format`) and returns parsed JSON.

    Refusals are re-run server-side on Anthropic's recommended fallback model
    (`fallbacks: "default"`); if the whole chain still refuses, an LLMError is raised.
    """

    def __init__(self, cfg: Config):
        if not secret("ANTHROPIC_API_KEY"):
            raise LLMError("ANTHROPIC_API_KEY is not set (add it to .env)")
        self.client = anthropic.Anthropic(max_retries=int(cfg.get("llm.max_retries", 4)))
        self.model = cfg.get("llm.model", "claude-opus-5-5")
        self.effort = cfg.get("llm.effort", "medium")

    def __call__(self, system: str, user: str, schema: dict, max_tokens: int = 16000) -> dict:
        for attempt in range(3):
            try:
                with self.client.beta.messages.stream(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    thinking={"type": "adaptive"},
                    output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                ) as stream:
                    msg = stream.get_final_message()
            except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.OverloadedError,
                    anthropic.APIConnectionError) as exc:
                wait = 10 * (attempt + 1)
                log.warning("Claude API transient error (%s); retrying in %ss", type(exc).__name__, wait)
                time.sleep(wait)
                continue
            except anthropic.APIStatusError as exc:
                raise LLMError(f"Claude API error {exc.status_code}: {exc.message}") from exc

            if msg.stop_reason == "refusal":
                raise LLMError("Claude declined this request")
            if msg.stop_reason == "max_tokens":
                raise LLMError("Claude response was cut off (max_tokens); try a smaller llm.window_minutes")
            text = next((b.text for b in msg.content if b.type == "text"), "")
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise LLMError(f"Claude returned invalid JSON: {exc}") from exc
        raise LLMError("Claude API unavailable after retries")
