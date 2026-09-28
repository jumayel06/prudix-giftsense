"""recommend(): retrieval → rerank, with small-catalog mode."""
import json
from unittest.mock import AsyncMock

import pytest

from app.llm import LLMResponse
from app.services.gifting.catalog import CatalogProduct
from app.services.gifting.embeddings import FakeEmbedder
from app.services.gifting.pipeline import SMALL_CATALOG_MAX, recommend
from app.services.gifting.profile import GiftProfile, embedding_text
from app.services.gifting.retrieval import IndexedProduct, Intake

INTAKE = Intake(recipient="friend", occasion="birthday", budget_band="25_50", vibes=["cozy"])


FACT = "hand-thrown stoneware"


def catalog(n, price=35.0):
    emb = FakeEmbedder()
    items = []
    for i in range(n):
        p = CatalogProduct(product_id=str(i), title=f"Gift {i}", product_type=f"Type{i % 7}", price_min=price, price_max=price)
        prof = GiftProfile(giftable=0.9, recipients=["friend"], occasions=["birthday"], vibes=["cozy"],
                           gift_pitch=f"Gift {i} for a friend.", facts=[FACT])
        items.append(IndexedProduct(p, prof, emb.embed_sync(embedding_text(p, prof))))
    return items


def chat_picking(ids):
    payload = {"picks": [{"product_id": i, "fact": FACT, "reason": f"A lovely gift {i}."} for i in ids]}
    return AsyncMock(return_value=LLMResponse(text=json.dumps(payload), input_tokens=1800, output_tokens=200))


class SpyEmbedder(FakeEmbedder):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        return await super().embed(texts)


@pytest.mark.asyncio
async def test_small_catalog_skips_embedding_and_sends_every_eligible_product():
    items = catalog(20)
    emb = SpyEmbedder()
    chat = chat_picking(["3", "7", "11"])
    result = await recommend(INTAKE, items, embedder=emb, model="claude-haiku-4-5", chat_fn=chat)
    assert result.mode == "small_catalog"
    assert emb.calls == 0
    prompt = chat.await_args.kwargs["prompt"]
    assert all(f"product_id: {i} |" in prompt for i in range(20))
    assert [p.product.product_id for p in result.picks] == ["3", "7", "11"]


@pytest.mark.asyncio
async def test_large_catalog_embeds_query_once_and_shortlists():
    items = catalog(SMALL_CATALOG_MAX + 40)
    emb = SpyEmbedder()
    chat = chat_picking(["1", "2", "3"])
    result = await recommend(INTAKE, items, embedder=emb, model="claude-haiku-4-5", chat_fn=chat)
    assert result.mode == "vector"
    assert emb.calls == 1
    assert chat.await_args.kwargs["prompt"].count("product_id:") == 12


@pytest.mark.asyncio
async def test_nothing_in_budget_returns_empty_without_llm():
    chat = chat_picking([])
    result = await recommend(INTAKE, catalog(10, price=500), embedder=FakeEmbedder(),
                             model="claude-haiku-4-5", chat_fn=chat)
    assert result.picks == []
    chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_budget_left_uses_templates_without_llm():
    chat = chat_picking(["1"])
    result = await recommend(INTAKE, catalog(10), embedder=FakeEmbedder(), model="claude-haiku-4-5",
                             chat_fn=chat, use_llm=False)
    chat.assert_not_awaited()
    assert result.used_fallback and len(result.picks) == 5


@pytest.mark.asyncio
async def test_every_pick_is_within_budget_even_if_llm_misbehaves():
    items = catalog(10) + catalog(1, price=999)  # id "0" duplicated at 999 is filtered before the LLM
    chat = chat_picking(["0", "1", "2"])
    result = await recommend(INTAKE, items, embedder=FakeEmbedder(), model="claude-haiku-4-5", chat_fn=chat)
    assert all(p.product.price_min <= 50 for p in result.picks)
