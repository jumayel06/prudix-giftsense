"""Single source for LLM calls (copied from Prudix Commerce).

Route code imports `chat` from here; tests patch `app.<module>.chat` and return
an `LLMResponse`. Never patch `openai` / `anthropic` directly.

Request shaping (output-cap field, temperature, thinking/reasoning off) comes
from each model's capability flags in app/ai_models.py, so adding a model is a
registry change. A "model not found" error (the provider retired it before we
updated the registry) is retried once on the model's replacement or fallback.
"""
import json
from dataclasses import dataclass

import anthropic as _anthropic
import openai as _openai
import structlog
from openai import AsyncOpenAI

from app.ai_models import MODELS, price_per_token
from core.config import settings

logger = structlog.get_logger()

_openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
_anthropic_client = _anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

# USD per token, derived from the registry (kept for callers that read it).
MODEL_COSTS = {m: dict(zip(("input", "output"), price_per_token(m))) for m in MODELS}

# Capabilities for a model missing from the registry (shouldn't happen in app
# code; keeps ad-hoc calls working).
_DEFAULT_CAPS = {"max_tokens_param": "max_tokens", "temperature": "always", "thinking_off": {}, "thinking_on": {}}

# Per-request timeout (seconds). The SDK defaults (10 minutes, retried) let one
# stalled request freeze a caller; seen in the 2026-09-28 eval. Shopper-facing
# callers pass much shorter values (gift search: rerank.RERANK_TIMEOUT_SECS).
DEFAULT_TIMEOUT_SECS = 60.0

_MODEL_GONE = (_anthropic.NotFoundError, _openai.NotFoundError)


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    model: str = ""  # the model that actually answered (may be a fallback)


def calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    cin, cout = price_per_token(model)
    return round(input_tokens * cin + output_tokens * cout, 8)


def _caps(model: str) -> dict:
    return (MODELS.get(model) or {}).get("caps", _DEFAULT_CAPS)


def _provider(model: str) -> str:
    spec = MODELS.get(model)
    if spec:
        return spec["provider"]
    return "anthropic" if model.startswith("claude-") else "openai"


async def chat(
    model: str,
    system: str,
    prompt: str,
    max_tokens: int = 2000,
    temperature: float = 0.8,
    json_mode: bool = False,
    thinking: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECS,
) -> LLMResponse:
    """`thinking=True` lets models that support it think (slower, costlier,
    sometimes better); off by default for our short structured tasks."""
    try:
        return await _call(model, system, prompt, max_tokens, temperature, json_mode, thinking, timeout)
    except _MODEL_GONE as e:
        spec = MODELS.get(model) or {}
        backup = spec.get("replacement") or spec.get("fallback")
        if not backup:
            raise
        logger.error("llm_model_gone_fallback", model=model, fallback=backup, error=str(e)[:200])
        return await _call(backup, system, prompt, max_tokens, temperature, json_mode, thinking, timeout)


async def _call(model, system, prompt, max_tokens, temperature, json_mode, thinking, timeout) -> LLMResponse:
    caps = _caps(model)
    extra = dict(caps["thinking_on"] if thinking else caps["thinking_off"])
    extra[caps["max_tokens_param"]] = max_tokens
    if caps["temperature"] == "always" or (caps["temperature"] == "thinking_off" and not thinking):
        extra["temperature"] = temperature
    if _provider(model) == "anthropic":
        return await _claude_chat(model, system, prompt, json_mode, timeout, extra)
    return await _openai_chat(model, system, prompt, json_mode, timeout, extra)


async def _openai_chat(model, system, prompt, json_mode, timeout, extra) -> LLMResponse:
    if json_mode:
        extra["response_format"] = {"type": "json_object"}
    response = await _openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
        timeout=timeout,
        **extra,
    )
    usage = response.usage
    text = response.choices[0].message.content or ""
    return LLMResponse(
        text=extract_json(text) if json_mode else text,
        input_tokens=usage.prompt_tokens if usage else 0,
        output_tokens=usage.completion_tokens if usage else 0,
        model=model,
    )


def extract_json(text: str) -> str:
    """Best effort: return the JSON object inside a model reply (bare, fenced,
    or wrapped in prose). Returns the input unchanged when none is found, so
    callers' own json.loads still reports the failure."""
    stripped = _strip_markdown_fences(text or "")
    try:
        json.loads(stripped)
        return stripped
    except ValueError:
        pass
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        candidate = stripped[start:end + 1]
        try:
            json.loads(candidate)
            return candidate
        except ValueError:
            pass
    return text


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    return text.strip()


async def _claude_chat(model, system, prompt, json_mode, timeout, extra) -> LLMResponse:
    full_system = system
    if json_mode:
        full_system = system + "\n\nRespond with valid JSON only. Do not include markdown, code fences, or any text outside the JSON object."

    response = await _anthropic_client.messages.create(
        model=model,
        system=full_system,
        messages=[{"role": "user", "content": prompt}],
        timeout=timeout,
        **extra,
    )
    usage = response.usage
    # Skip non-text blocks (e.g. thinking) and join the text ones.
    text = "".join(
        getattr(block, "text", "") for block in (response.content or [])
        if getattr(block, "type", "text") == "text"
    )
    if json_mode:
        text = extract_json(text)
    return LLMResponse(
        text=text,
        input_tokens=usage.input_tokens if usage else 0,
        output_tokens=usage.output_tokens if usage else 0,
        model=model,
    )
