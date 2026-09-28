"""Rerank + reasons: one LLM call, validated output, template fallback."""
import json
from unittest.mock import AsyncMock

import pytest

from app.llm import LLMResponse
from app.services.gifting.catalog import CatalogProduct
from app.services.gifting.profile import GiftProfile
from app.services.gifting.rerank import (
    RERANK_SYSTEM_PROMPT, rerank, template_reason, validate_reason,
)
from app.services.gifting.retrieval import Candidate, Intake

INTAKE = Intake(recipient="parent", occasion="birthday", budget_band="25_50", vibes=["cozy", "relaxing"])


def cand(pid, title, price=40.0, vibes=("cozy",), facts=("soy wax",), score=0.8):
    p = CatalogProduct(product_id=pid, title=title, price_min=price, price_max=price, product_type="Candles")
    prof = GiftProfile(giftable=0.9, recipients=["parent"], occasions=["birthday"], vibes=list(vibes),
                       gift_pitch=f"{title} for cozy nights.", facts=list(facts))
    return Candidate(p, prof, [], score, 0.5)


CANDS = [cand("a", "Lavender Candle", 38), cand("b", "Plush Robe", 49), cand("c", "Tea Sampler", 28),
         cand("d", "Bath Salts", 26), cand("e", "Reading Light", 35), cand("f", "Mug", 22)]


def llm(payload, tokens=(2500, 350)):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return AsyncMock(return_value=LLMResponse(text=text, input_tokens=tokens[0], output_tokens=tokens[1]))


@pytest.mark.asyncio
async def test_valid_picks_are_returned_in_llm_order_with_reasons():
    chat = llm({"picks": [
        {"product_id": "b", "reason": "A plush robe for slow, cozy birthday mornings."},
        {"product_id": "a", "reason": "Lavender scent to help a busy parent unwind."},
        {"product_id": "c", "reason": "A tea sampler for quiet afternoons."},
    ]})
    result = await rerank(INTAKE, CANDS, model="claude-sonnet-5", chat_fn=chat)
    assert [p.product.product_id for p in result.picks] == ["b", "a", "c"]
    assert all(p.source == "ai" for p in result.picks)
    assert result.used_fallback is False
    assert (result.input_tokens, result.output_tokens) == (2500, 350)
    assert chat.await_args.kwargs["model"] == "claude-sonnet-5"


@pytest.mark.asyncio
async def test_invented_and_duplicate_products_are_dropped_and_topped_up():
    chat = llm({"picks": [
        {"product_id": "zzz", "reason": "Not in the catalog."},
        {"product_id": "a", "reason": "Lavender scent to unwind."},
        {"product_id": "a", "reason": "Duplicate."},
    ]})
    result = await rerank(INTAKE, CANDS, model="claude-haiku-4-5", chat_fn=chat)
    ids = [p.product.product_id for p in result.picks]
    assert "zzz" not in ids and ids.count("a") == 1
    assert len(ids) >= 3                       # topped up from the shortlist
    assert result.picks[0].source == "ai"
    assert {p.source for p in result.picks[1:]} == {"template"}


@pytest.mark.asyncio
async def test_llm_failure_returns_template_picks():
    chat = AsyncMock(side_effect=RuntimeError("overloaded"))
    result = await rerank(INTAKE, CANDS, model="claude-haiku-4-5", chat_fn=chat)
    assert result.used_fallback is True
    assert [p.product.product_id for p in result.picks] == ["a", "b", "c", "d", "e"]
    assert all(p.source == "template" and p.reason for p in result.picks)


@pytest.mark.asyncio
async def test_unparseable_output_returns_template_picks():
    result = await rerank(INTAKE, CANDS, model="claude-haiku-4-5", chat_fn=llm("sorry, I can't"))
    assert result.used_fallback is True and len(result.picks) == 5


@pytest.mark.asyncio
async def test_no_llm_call_when_generation_budget_disallows():
    chat = llm({"picks": []})
    result = await rerank(INTAKE, CANDS, model="claude-haiku-4-5", chat_fn=chat, use_llm=False)
    chat.assert_not_awaited()
    assert result.used_fallback is True and len(result.picks) == 5


@pytest.mark.asyncio
async def test_empty_shortlist_returns_no_picks_without_calling_llm():
    chat = llm({"picks": []})
    result = await rerank(INTAKE, [], model="claude-haiku-4-5", chat_fn=chat)
    chat.assert_not_awaited()
    assert result.picks == []


@pytest.mark.parametrize("reason,ok", [
    ("A lavender candle for slow evenings.", True),
    ("Only $38, well within your budget.", True),        # matches the real price
    ("A steal at $12.", False),                          # price the product doesn't have
    ("See https://example.com for more.", False),        # links
    ("", False),
    ("x" * 300, False),
])
def test_validate_reason(reason, ok):
    c = cand("a", "Lavender Candle", 38)
    assert (validate_reason(reason, c) is not None) is ok


def test_template_reason_mentions_budget_and_matching_vibes():
    text = template_reason(INTAKE, cand("a", "Lavender Candle", 38, vibes=("cozy", "relaxing")))
    assert "$25–50" in text and "cozy" in text.lower()


def test_prompt_tells_model_to_use_only_given_facts():
    assert "only" in RERANK_SYSTEM_PROMPT.lower() and "facts" in RERANK_SYSTEM_PROMPT.lower()


@pytest.mark.asyncio
async def test_rerank_uses_a_short_timeout_for_shoppers():
    chat = llm({"picks": []})
    await rerank(INTAKE, CANDS, model="claude-haiku-4-5", chat_fn=chat)
    from app.services.gifting.rerank import RERANK_TIMEOUT_SECS
    assert chat.await_args.kwargs["timeout"] == RERANK_TIMEOUT_SECS <= 10


def test_prompt_forbids_stretching_a_product_to_fit_the_note_or_vibes():
    # Eval 2026-09-28: most unfaithful reasons tied unrelated products to the
    # shopper's note ("marathon") or a vibe ("sentimental") the listing didn't support.
    p = RERANK_SYSTEM_PROMPT.lower()
    assert "shopper's note" in p and "don't claim" in p
