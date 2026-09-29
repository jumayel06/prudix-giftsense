"""app/llm.py: provider-specific request shaping + tolerant JSON extraction."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app import llm


def _anthropic_resp(text, blocks=None):
    content = blocks or [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(content=content, usage=SimpleNamespace(input_tokens=10, output_tokens=5))


@pytest.mark.asyncio
async def test_sonnet5_disables_thinking_and_omits_temperature_by_default():
    create = AsyncMock(return_value=_anthropic_resp('{"ok": 1}'))
    with patch.object(llm._anthropic_client.messages, "create", create):
        await llm.chat("claude-sonnet-5", "sys", "hi", json_mode=True)
    kwargs = create.await_args.kwargs
    assert kwargs["thinking"] == {"type": "disabled"}
    assert "temperature" not in kwargs


@pytest.mark.asyncio
async def test_sonnet5_can_opt_into_adaptive_thinking():
    create = AsyncMock(return_value=_anthropic_resp('{"ok": 1}'))
    with patch.object(llm._anthropic_client.messages, "create", create):
        await llm.chat("claude-sonnet-5", "sys", "hi", thinking=True)
    assert "thinking" not in create.await_args.kwargs  # omitted → adaptive


@pytest.mark.asyncio
async def test_haiku_keeps_temperature_and_sends_no_thinking_param():
    create = AsyncMock(return_value=_anthropic_resp("hello"))
    with patch.object(llm._anthropic_client.messages, "create", create):
        await llm.chat("claude-haiku-4-5", "sys", "hi", temperature=0.3)
    kwargs = create.await_args.kwargs
    assert kwargs["temperature"] == 0.3 and "thinking" not in kwargs


@pytest.mark.asyncio
async def test_thinking_blocks_are_ignored_in_text():
    blocks = [SimpleNamespace(type="thinking", thinking="let me think"),
              SimpleNamespace(type="text", text='{"a": 1}')]
    create = AsyncMock(return_value=_anthropic_resp(None, blocks))
    with patch.object(llm._anthropic_client.messages, "create", create):
        resp = await llm.chat("claude-sonnet-5", "sys", "hi", json_mode=True, thinking=True)
    assert resp.text == '{"a": 1}'


@pytest.mark.parametrize("raw,expected", [
    ('{"a": 1}', '{"a": 1}'),
    ('```json\n{"a": 1}\n```', '{"a": 1}'),
    ('Here is the result:\n{"a": [1, 2]}\nHope that helps.', '{"a": [1, 2]}'),
    ('no json at all', 'no json at all'),
])
def test_extract_json(raw, expected):
    assert llm.extract_json(raw) == expected


@pytest.mark.asyncio
async def test_timeout_is_passed_to_both_providers():
    create = AsyncMock(return_value=_anthropic_resp("hi"))
    with patch.object(llm._anthropic_client.messages, "create", create):
        await llm.chat("claude-haiku-4-5", "sys", "hi", timeout=8.0)
    assert create.await_args.kwargs["timeout"] == 8.0

    choice = SimpleNamespace(message=SimpleNamespace(content="hi"))
    oa = AsyncMock(return_value=SimpleNamespace(choices=[choice], usage=None))
    with patch.object(llm._openai_client.chat.completions, "create", oa):
        await llm.chat("gpt-4o-mini", "sys", "hi", timeout=8.0)
    assert oa.await_args.kwargs["timeout"] == 8.0


@pytest.mark.asyncio
async def test_default_timeout_is_bounded():
    create = AsyncMock(return_value=_anthropic_resp("hi"))
    with patch.object(llm._anthropic_client.messages, "create", create):
        await llm.chat("claude-haiku-4-5", "sys", "hi")
    assert 0 < create.await_args.kwargs["timeout"] <= 120


def _openai_mock(text="hi"):
    choice = SimpleNamespace(message=SimpleNamespace(content=text))
    return AsyncMock(return_value=SimpleNamespace(choices=[choice], usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5)))


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6-sol"])
async def test_gpt6_runs_without_reasoning_by_default(model):
    # GPT-6 models reason by default (medium): slower and billed as output.
    # Our short structured calls turn it off; temperature is only allowed then.
    oa = _openai_mock('{"a": 1}')
    with patch.object(llm._openai_client.chat.completions, "create", oa):
        resp = await llm.chat(model, "sys", "hi", max_tokens=700, temperature=0.4, json_mode=True)
    kw = oa.await_args.kwargs
    assert kw["reasoning_effort"] == "none" and kw["temperature"] == 0.4
    assert kw["max_completion_tokens"] == 700 and "max_tokens" not in kw
    assert kw["response_format"] == {"type": "json_object"} and resp.text == '{"a": 1}'


@pytest.mark.asyncio
async def test_gpt6_thinking_opt_in_drops_temperature():
    oa = _openai_mock()
    with patch.object(llm._openai_client.chat.completions, "create", oa):
        await llm.chat("gpt-6-sol", "sys", "hi", temperature=0.4, thinking=True)
    kw = oa.await_args.kwargs
    assert kw["reasoning_effort"] == "medium" and "temperature" not in kw


@pytest.mark.asyncio
async def test_older_openai_models_unchanged():
    oa = _openai_mock()
    with patch.object(llm._openai_client.chat.completions, "create", oa):
        await llm.chat("gpt-4o-mini", "sys", "hi", max_tokens=50, temperature=0.4)
    kw = oa.await_args.kwargs
    assert kw["max_tokens"] == 50 and kw["temperature"] == 0.4 and "reasoning_effort" not in kw


def test_gpt6_prices():
    assert llm.calc_cost("gpt-6-luna", 1_000_000, 1_000_000) == pytest.approx(0.60)
    assert llm.calc_cost("gpt-6-sol", 1_000_000, 1_000_000) == pytest.approx(12.0)


@pytest.mark.asyncio
async def test_sonnet55_turns_thinking_off_with_between_tools():
    # Sonnet 5.5 rejects {"type": "disabled"} (400); "between_tools" is its
    # lowest setting and, with no tools in the request, means no thinking.
    create = AsyncMock(return_value=_anthropic_resp('{"ok": 1}'))
    with patch.object(llm._anthropic_client.messages, "create", create):
        await llm.chat("claude-sonnet-5-5", "sys", "hi", temperature=0.4, json_mode=True)
    kwargs = create.await_args.kwargs
    assert kwargs["thinking"] == {"type": "between_tools"}
    assert "temperature" not in kwargs  # non-default sampling params → 400


@pytest.mark.asyncio
async def test_sonnet55_thinking_opt_in_is_adaptive():
    create = AsyncMock(return_value=_anthropic_resp('{"ok": 1}'))
    with patch.object(llm._anthropic_client.messages, "create", create):
        await llm.chat("claude-sonnet-5-5", "sys", "hi", thinking=True)
    assert "thinking" not in create.await_args.kwargs


def test_sonnet55_price():
    assert llm.calc_cost("claude-sonnet-5-5", 1_000_000, 1_000_000) == pytest.approx(12.0)


@pytest.mark.asyncio
async def test_model_gone_retries_once_on_its_fallback():
    # A provider retired the model before we updated app/ai_models.py.
    import anthropic
    gone = anthropic.NotFoundError("model not found", response=SimpleNamespace(status_code=404, headers={},
                                   request=None), body=None)
    create = AsyncMock(side_effect=gone)
    oa = _openai_mock('{"ok": 1}')
    with patch.object(llm._anthropic_client.messages, "create", create), \
         patch.object(llm._openai_client.chat.completions, "create", oa):
        resp = await llm.chat("claude-sonnet-5", "sys", "hi", json_mode=True)
    assert resp.model == "gpt-6-sol" and resp.text == '{"ok": 1}'   # Sonnet 5's fallback
    assert oa.await_args.kwargs["model"] == "gpt-6-sol"


@pytest.mark.asyncio
async def test_response_records_the_model_that_answered():
    create = AsyncMock(return_value=_anthropic_resp("hi"))
    with patch.object(llm._anthropic_client.messages, "create", create):
        resp = await llm.chat("claude-sonnet-5", "sys", "hi")
    assert resp.model == "claude-sonnet-5"
