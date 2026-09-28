"""Single source for LLM calls (copied from Prudix Commerce).

Route code imports `chat` from here; tests patch `app.<module>.chat` and return
an `LLMResponse`. Never patch `openai` / `anthropic` directly.
"""
import json
from dataclasses import dataclass

import anthropic as _anthropic
from openai import AsyncOpenAI

from core.config import settings

_openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
_anthropic_client = _anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

# USD per token (list prices per million / 1e6).
MODEL_COSTS = {
    "gpt-4o-mini":      {"input": 0.00000015, "output": 0.00000060},
    "claude-haiku-4-5": {"input": 0.000001,   "output": 0.000005},
    "gpt-4.1":          {"input": 0.000002,   "output": 0.000008},
    "claude-sonnet-5":  {"input": 0.000002,   "output": 0.000010},
    # GPT-6 (2026): evaluated as replacements for gpt-4o-mini / gpt-4.1.
    "gpt-6-luna":       {"input": 0.0000001,  "output": 0.0000005},
    "gpt-6-sol":        {"input": 0.000002,   "output": 0.000010},
}

# OpenAI reasoning models: they reason by default (billed as output, slower),
# take `max_completion_tokens` instead of `max_tokens`, and accept temperature
# only with reasoning_effort "none". We run them with "none" unless a caller
# opts into thinking.
_OPENAI_REASONING_PREFIXES = ("gpt-6",)

# Claude models that reject sampling parameters (temperature/top_p/top_k)
# with a 400. Sonnet 5 is one of them; Haiku 4.5 still accepts temperature.
_NO_SAMPLING_PARAMS = {"claude-sonnet-5"}

# Claude models that think adaptively when `thinking` is omitted. Hidden
# thinking is billed as output and adds seconds of latency (measured in the
# 2026-09-28 eval: Sonnet 5 rerank 7.3s p95), so our short structured calls
# (gift picks, notes, profiles) disable it unless a caller opts in.
_ADAPTIVE_BY_DEFAULT = {"claude-sonnet-5"}

# Per-request timeout (seconds). The SDK defaults (10 minutes, retried) let one
# stalled request freeze a caller; seen in the 2026-09-28 eval. Shopper-facing
# callers pass much shorter values (gift search: rerank.RERANK_TIMEOUT_SECS).
DEFAULT_TIMEOUT_SECS = 60.0


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int


def calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    c = MODEL_COSTS.get(model, MODEL_COSTS["gpt-4o-mini"])
    return round(input_tokens * c["input"] + output_tokens * c["output"], 8)


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
    """`thinking=True` lets models that support it think adaptively (slower,
    costlier, sometimes better); default off for our short structured tasks."""
    if model.startswith("claude-"):
        return await _claude_chat(model, system, prompt, max_tokens, temperature, json_mode, thinking, timeout)
    return await _openai_chat(model, system, prompt, max_tokens, temperature, json_mode, timeout, thinking)


async def _openai_chat(model, system, prompt, max_tokens, temperature, json_mode,
                       timeout=DEFAULT_TIMEOUT_SECS, thinking=False) -> LLMResponse:
    kwargs = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if model.startswith(_OPENAI_REASONING_PREFIXES):
        kwargs["max_completion_tokens"] = max_tokens
        kwargs["reasoning_effort"] = "medium" if thinking else "none"
        if not thinking:
            kwargs["temperature"] = temperature
    else:
        kwargs["max_tokens"] = max_tokens
        kwargs["temperature"] = temperature

    response = await _openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
        timeout=timeout,
        **kwargs,
    )
    usage = response.usage
    text = response.choices[0].message.content or ""
    return LLMResponse(
        text=extract_json(text) if json_mode else text,
        input_tokens=usage.prompt_tokens if usage else 0,
        output_tokens=usage.completion_tokens if usage else 0,
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


async def _claude_chat(model, system, prompt, max_tokens, temperature, json_mode, thinking=False,
                       timeout=DEFAULT_TIMEOUT_SECS) -> LLMResponse:
    full_system = system
    if json_mode:
        full_system = system + "\n\nRespond with valid JSON only. Do not include markdown, code fences, or any text outside the JSON object."

    kwargs = {}
    if model not in _NO_SAMPLING_PARAMS:
        kwargs["temperature"] = temperature
    if model in _ADAPTIVE_BY_DEFAULT and not thinking:
        kwargs["thinking"] = {"type": "disabled"}

    response = await _anthropic_client.messages.create(
        model=model,
        system=full_system,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        timeout=timeout,
        **kwargs,
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
    )
