"""Gift-profile enrichment: one LLM call per product version (mocked here)."""
import json
from unittest.mock import AsyncMock

import pytest

from app.llm import LLMResponse
from app.services.gifting import vocab
from app.services.gifting.catalog import CatalogProduct
from app.services.gifting.enrichment import ENRICHMENT_SYSTEM_PROMPT, enrich_product

PRODUCT = CatalogProduct(product_id="1", title="Merino Throw", description="Warm wool throw.",
                         product_type="Blankets", tags=["cozy"], price_min=89, price_max=89)

GOOD = {
    "giftable": 0.9, "recipients": ["parent"], "occasions": ["birthday"], "vibes": ["cozy"],
    "interests": ["home"], "age_band": "adult", "gift_pitch": "A warm throw.", "facts": ["wool"],
}


@pytest.mark.asyncio
async def test_enrich_returns_validated_profile_and_usage():
    chat = AsyncMock(return_value=LLMResponse(text=json.dumps(GOOD), input_tokens=900, output_tokens=150))
    result = await enrich_product(PRODUCT, model="gpt-6-luna", chat_fn=chat)
    assert result.profile.vibes == ["cozy"]
    assert result.used_fallback is False
    assert (result.input_tokens, result.output_tokens) == (900, 150)
    kwargs = chat.await_args.kwargs
    assert kwargs["json_mode"] is True
    assert kwargs["model"] == "gpt-6-luna"
    assert "Merino Throw" in kwargs["prompt"]


@pytest.mark.asyncio
async def test_invalid_json_falls_back_to_tag_profile():
    chat = AsyncMock(return_value=LLMResponse(text="not json", input_tokens=900, output_tokens=5))
    result = await enrich_product(PRODUCT, model="gpt-6-luna", chat_fn=chat)
    assert result.used_fallback is True
    assert result.profile.vibes == ["cozy"]


@pytest.mark.asyncio
async def test_llm_error_falls_back_without_raising():
    chat = AsyncMock(side_effect=RuntimeError("timeout"))
    result = await enrich_product(PRODUCT, model="gpt-6-luna", chat_fn=chat)
    assert result.used_fallback is True
    assert (result.input_tokens, result.output_tokens) == (0, 0)


def test_system_prompt_lists_every_vocabulary_value_and_bias_guard():
    for values in (vocab.RECIPIENTS, vocab.OCCASIONS, vocab.VIBES, vocab.AGE_BANDS):
        for v in values:
            assert v in ENRICHMENT_SYSTEM_PROMPT, v
    assert "stereotype" in ENRICHMENT_SYSTEM_PROMPT.lower()
    assert "only" in ENRICHMENT_SYSTEM_PROMPT.lower() and "facts" in ENRICHMENT_SYSTEM_PROMPT.lower()
