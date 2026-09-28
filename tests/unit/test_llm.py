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
