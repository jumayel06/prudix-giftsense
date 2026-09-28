"""Eval tooling: spending ledger + sample catalog generator (LLM mocked)."""
import json
from unittest.mock import AsyncMock

import pytest

from app.llm import LLMResponse
from evals.catalogs import STORES, generate_catalog
from evals.ledger import BudgetExceeded, Ledger


def test_ledger_tracks_spend_and_refuses_calls_over_the_cap():
    ledger = Ledger(cap_usd=0.01)
    ledger.charge("step", "claude-haiku-4-5", 2500, 400)          # $0.0045
    assert ledger.spent == pytest.approx(0.0045)
    ledger.check(0.005)                                            # 0.0095 ≤ 0.01 → fine
    with pytest.raises(BudgetExceeded):
        ledger.check(0.006)                                        # would exceed
    assert ledger.by_step()["step"] == pytest.approx(0.0045)


def _chunk(n, start=0, bad=0):
    items = [{"title": f"Product {start + i}", "product_type": "Candles", "vendor": "Wick & Co",
              "description_html": "<p>Hand-poured soy candle.</p>", "tags": ["candle", "cozy"],
              "price_min": 24, "price_max": 24} for i in range(n)]
    items += [{"title": ""}] * bad          # invalid rows are skipped
    return LLMResponse(text=json.dumps({"products": items}), input_tokens=600, output_tokens=n * 250)


@pytest.mark.asyncio
async def test_generate_catalog_chunks_dedupes_and_assigns_ids():
    spec = STORES["candles"]
    responses = [_chunk(20, 0, bad=2), _chunk(20, 10)]   # second chunk repeats 10 titles
    chat = AsyncMock(side_effect=responses + [_chunk(20, 30)] * 5)
    ledger = Ledger(cap_usd=5)
    products = await generate_catalog("candles", spec, chat_fn=chat, ledger=ledger, count=25)
    assert len(products) == 25
    titles = [p["title"] for p in products]
    assert len(set(titles)) == 25
    assert len({p["product_id"] for p in products}) == 25
    assert ledger.spent > 0


@pytest.mark.asyncio
async def test_generate_catalog_stops_at_budget():
    chat = AsyncMock(side_effect=[_chunk(20, i * 20) for i in range(10)])
    with pytest.raises(BudgetExceeded):
        await generate_catalog("general", STORES["general"], chat_fn=chat, ledger=Ledger(cap_usd=0.01), count=200)


def test_store_specs_cover_the_five_store_types_with_non_gift_items():
    assert set(STORES) == {"candles", "jewelry", "toys", "kitchen", "general"}
    for spec in STORES.values():
        assert spec["categories"] and spec["non_gift_examples"]


@pytest.mark.asyncio
async def test_transient_errors_are_retried():
    import anthropic
    import httpx
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    chat = AsyncMock(side_effect=[err, _chunk(20), _chunk(20, 20)])
    products = await generate_catalog("candles", STORES["candles"], chat_fn=chat, ledger=Ledger(5),
                                      count=25, retry_delay=0)
    assert len(products) == 25


@pytest.mark.asyncio
async def test_resumes_from_checkpoint_and_reports_progress():
    existing = [{"product_id": f"candles-{i + 1:04d}", "title": f"Old {i}", "product_type": "Candles",
                 "vendor": "V", "description": "", "tags": [], "price_min": 20.0, "price_max": 20.0,
                 "available": True} for i in range(15)]
    saved = []
    chat = AsyncMock(side_effect=[_chunk(20, 100)])
    products = await generate_catalog("candles", STORES["candles"], chat_fn=chat, ledger=Ledger(5), count=25,
                                      existing=existing, on_progress=lambda ps: saved.append(len(ps)))
    assert len(products) == 25
    assert products[:15] == existing
    assert len({p["product_id"] for p in products}) == 25
    assert saved and saved[-1] == 25
    assert "Write 10 new products" in chat.await_args.kwargs["prompt"]
