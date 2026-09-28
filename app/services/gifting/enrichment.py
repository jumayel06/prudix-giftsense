"""Gift-profile enrichment: one LLM call per product version
(docs/TECHNICAL_PLAN.md §4.2). Initial catalog reads go through the Batch API
in production (week 3); this is the single-product path, also used by the
offline eval.

Always runs on the fixed catalog model (Haiku 4.5), not the merchant's chosen
model, and doesn't consume merchant generations (§8.2).
"""
import json
from dataclasses import dataclass
from typing import Awaitable, Callable

import structlog

from app.llm import LLMResponse, chat
from app.services.gifting import vocab
from app.services.gifting.catalog import CatalogProduct
from app.services.gifting.profile import GiftProfile, fallback_profile, parse_profile, product_source_text

logger = structlog.get_logger()

CATALOG_MODEL = "claude-haiku-4-5"
PROMPT_VERSION = "enrich-v1"

ChatFn = Callable[..., Awaitable[LLMResponse]]

ENRICHMENT_SYSTEM_PROMPT = f"""You describe a store product as a potential gift, for a gift-finder that matches products to shoppers.

Return one JSON object with exactly these keys:
- "giftable": number 0-1, how plausible this is as a gift at all (refills, spare parts, gift cards, shipping items are low).
- "recipients": list chosen ONLY from: {", ".join(vocab.RECIPIENTS)}
- "occasions": list chosen ONLY from: {", ".join(vocab.OCCASIONS)}
- "vibes": list of up to 4 chosen ONLY from: {", ".join(vocab.VIBES)}
- "interests": up to 6 short lowercase hobby or interest words (e.g. "coffee", "hiking").
- "age_band": one of: {", ".join(vocab.AGE_BANDS)}
- "gift_pitch": one sentence (max 25 words) on who would love this as a gift and why.
- "facts": up to 6 short, concrete product facts copied from the product information (material, size, what's included, how it's used).

Rules:
- Use only the product information given. Never invent materials, sizes, features, awards or prices.
- "facts" must be taken from the product information only.
- Do not infer recipients from gender stereotypes (e.g. do not assume kitchen items are for mothers or tools are for fathers). Only name a specific recipient when the product itself points to one; otherwise prefer broader recipients like "friend" or "anyone".
- If unsure about a list, leave it short or empty rather than guessing."""


@dataclass
class EnrichmentResult:
    profile: GiftProfile
    used_fallback: bool
    input_tokens: int = 0
    output_tokens: int = 0


async def enrich_product(
    product: CatalogProduct,
    model: str = CATALOG_MODEL,
    chat_fn: ChatFn = chat,
) -> EnrichmentResult:
    """Build a validated gift profile for one product. Never raises: on an LLM
    error or unusable output it falls back to a tag-based profile."""
    try:
        resp = await chat_fn(
            model=model,
            system=ENRICHMENT_SYSTEM_PROMPT,
            prompt=product_source_text(product),
            max_tokens=600,
            temperature=0.2,
            json_mode=True,
        )
    except Exception as e:  # noqa: BLE001 — enrichment must never break a catalog sync
        logger.warning("gift_enrichment_llm_failed", product_id=product.product_id, error=str(e))
        return EnrichmentResult(profile=fallback_profile(product), used_fallback=True)

    try:
        profile = parse_profile(json.loads(resp.text))
    except (ValueError, TypeError):
        profile = None
    if profile is None:
        logger.warning("gift_enrichment_unparseable", product_id=product.product_id)
        return EnrichmentResult(
            profile=fallback_profile(product), used_fallback=True,
            input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
        )
    if not profile.gift_pitch:
        profile.gift_pitch = fallback_profile(product).gift_pitch
    return EnrichmentResult(
        profile=profile, used_fallback=False,
        input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
    )
