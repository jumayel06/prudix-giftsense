"""Single source for LLM calls (copied from Prudix Commerce).

Route code imports `chat` from here; tests patch `app.<module>.chat` and return
an `LLMResponse`. Never patch `openai` / `anthropic` directly.
"""
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
}

# Claude models that reject sampling parameters (temperature/top_p/top_k)
# with a 400. Sonnet 5 is one of them; Haiku 4.5 still accepts temperature.
_NO_SAMPLING_PARAMS = {"claude-sonnet-5"}


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
) -> LLMResponse:
    if model.startswith("claude-"):
        return await _claude_chat(model, system, prompt, max_tokens, temperature, json_mode)
    return await _openai_chat(model, system, prompt, max_tokens, temperature, json_mode)


async def _openai_chat(model, system, prompt, max_tokens, temperature, json_mode) -> LLMResponse:
    kwargs = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    response = await _openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        **kwargs,
    )
    usage = response.usage
    return LLMResponse(
        text=response.choices[0].message.content or "",
        input_tokens=usage.prompt_tokens if usage else 0,
        output_tokens=usage.completion_tokens if usage else 0,
    )


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    return text.strip()


async def _claude_chat(model, system, prompt, max_tokens, temperature, json_mode) -> LLMResponse:
    full_system = system
    if json_mode:
        full_system = system + "\n\nRespond with valid JSON only. Do not include markdown, code fences, or any text outside the JSON object."

    kwargs = {}
    if model not in _NO_SAMPLING_PARAMS:
        kwargs["temperature"] = temperature

    response = await _anthropic_client.messages.create(
        model=model,
        system=full_system,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        **kwargs,
    )
    usage = response.usage
    # Skip non-text blocks (e.g. thinking) and join the text ones.
    text = "".join(
        getattr(block, "text", "") for block in (response.content or [])
        if getattr(block, "type", "text") == "text"
    )
    if json_mode:
        text = _strip_markdown_fences(text)
    return LLMResponse(
        text=text,
        input_tokens=usage.input_tokens if usage else 0,
        output_tokens=usage.output_tokens if usage else 0,
    )
