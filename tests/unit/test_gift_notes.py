"""AI gift-note drafting: prompt, cleaning, guards, template fallback."""
import json
from unittest.mock import AsyncMock

import pytest

from app.llm import LLMResponse
from app.services.gifting import notes as n

BASE = dict(recipient="parent", occasion="birthday", tone="warm", max_chars=250, banned_words=[],
            product_title="Lavender Candle", product_pitch="A calming candle for slow evenings.",
            product_facts=["soy wax"], name="")


def llm(*texts):
    return AsyncMock(side_effect=[LLMResponse(json.dumps({"note": t}), 300, 60, model="gpt-6-luna") for t in texts])


def clean_moderation():
    return AsyncMock(return_value=False)


@pytest.mark.asyncio
async def test_happy_path_returns_the_ai_note():
    r = await n.draft_note(**BASE, model="gpt-6-luna", chat_fn=llm("Happy birthday, Mom! Enjoy some calm evenings."),
                           moderate_fn=clean_moderation())
    assert r.source == "ai" and r.text == "Happy birthday, Mom! Enjoy some calm evenings."
    assert (r.input_tokens, r.output_tokens) == (300, 60)


@pytest.mark.asyncio
async def test_prompt_carries_context_and_rules():
    chat = llm("Happy birthday!")
    await n.draft_note(**{**BASE, "name": "Sam", "banned_words": ["cheap"], "tone": "playful"},
                       model="gpt-6-luna", chat_fn=chat, moderate_fn=clean_moderation())
    prompt = chat.await_args.kwargs["prompt"]
    system = chat.await_args.kwargs["system"]
    assert "Lavender Candle" in prompt and "Sam" in prompt and "Birthday" in prompt and "Parent" in prompt
    assert "cheap" in prompt and "250" in system and "playful" in system.lower()


def test_cleaning_removes_links_contacts_and_caps_length():
    text = n.clean_note('"Visit https://x.co or mail me@a.com, call +1 (555) 123-4567! ' + "word " * 80 + '"', 120)
    assert "http" not in text and "@" not in text and "555" not in text
    assert len(text) <= 120 and not text.endswith(" ")


@pytest.mark.asyncio
async def test_banned_word_retries_once_then_uses_template():
    chat = llm("Such a cheap thrill!", "Another cheap line")
    r = await n.draft_note(**{**BASE, "banned_words": ["cheap"]}, model="gpt-6-luna", chat_fn=chat,
                           moderate_fn=clean_moderation())
    assert chat.await_count == 2 and r.source == "template" and "cheap" not in r.text.lower()


@pytest.mark.asyncio
async def test_banned_word_fixed_on_retry():
    chat = llm("Such a cheap thrill!", "Such a lovely thrill!")
    r = await n.draft_note(**{**BASE, "banned_words": ["cheap"]}, model="gpt-6-luna", chat_fn=chat,
                           moderate_fn=clean_moderation())
    assert r.source == "ai" and r.text == "Such a lovely thrill!"


def test_banned_words_match_whole_words_only():
    assert n.contains_banned("A cheap gift", ["cheap"])
    assert not n.contains_banned("Cheaper than you think", ["cheap"])


@pytest.mark.asyncio
async def test_flagged_output_uses_template():
    r = await n.draft_note(**BASE, model="gpt-6-luna", chat_fn=llm("something awful"),
                           moderate_fn=AsyncMock(return_value=True))
    assert r.source == "template"


@pytest.mark.asyncio
async def test_llm_failure_uses_template():
    r = await n.draft_note(**BASE, model="gpt-6-luna", chat_fn=AsyncMock(side_effect=RuntimeError("down")),
                           moderate_fn=clean_moderation())
    assert r.source == "template" and r.text


@pytest.mark.parametrize("tone", ["warm", "elegant", "playful", "formal"])
def test_templates_exist_for_every_tone_and_use_the_name(tone):
    t = n.template_note(tone, "friend", "birthday", "Alex", 250)
    assert t and "Alex" in t and len(t) <= 250


def test_template_without_context_still_reads_well():
    assert n.template_note("warm", None, None, "", 250)
