"""Sample store catalogs for the offline evaluation, written by an LLM once and
saved to evals/data/catalogs/<store>.json (reruns reuse the file for free).

Each store mixes real gifts with a few poor gifts (refills, spare parts,
shipping protection) so the eval also checks the giftable filter.
"""
import json
from typing import Awaitable, Callable

from app.llm import LLMResponse, chat
from evals.ledger import Ledger

ChatFn = Callable[..., Awaitable[LLMResponse]]

GENERATOR_MODEL = "claude-haiku-4-5"
CHUNK = 20
EST_COST_PER_CHUNK = 0.03  # ~20 products × ~250 output tokens on Haiku, with margin

STORES = {
    "candles": {
        "count": 25, "about": "an independent hand-poured candle and home fragrance shop",
        "categories": {"Candles": (14, 48), "Wax Melts": (8, 18), "Reed Diffusers": (22, 55),
                       "Gift Sets": (35, 120), "Candle Accessories": (10, 30)},
        "non_gift_examples": ["replacement wick trimmer blade", "diffuser refill oil", "shipping protection"],
    },
    "jewelry": {
        "count": 120, "about": "a small-batch jewelry brand (sterling silver, gold vermeil, some solid gold)",
        "categories": {"Necklaces": (28, 260), "Earrings": (22, 180), "Rings": (30, 420), "Bracelets": (25, 220),
                       "Charms": (15, 60), "Personalized": (40, 190), "Jewelry Boxes": (20, 75)},
        "non_gift_examples": ["jewelry cleaning cloth refill", "ring sizer", "extended warranty"],
    },
    "toys": {
        "count": 300, "about": "a toy shop for babies, kids and teens (wooden toys, STEM kits, games, plush, crafts)",
        "categories": {"Baby & Toddler": (12, 80), "STEM Kits": (18, 120), "Board Games": (15, 70),
                       "Plush": (10, 45), "Arts & Crafts": (8, 55), "Outdoor Play": (15, 150), "Puzzles": (10, 40),
                       "Tech Toys": (25, 160)},
        "non_gift_examples": ["AA battery 4-pack", "replacement game pieces", "gift wrap add-on"],
    },
    "kitchen": {
        "count": 500, "about": "a specialty coffee, tea and kitchen goods store",
        "categories": {"Coffee Beans": (14, 40), "Tea": (10, 35), "Brewing Gear": (18, 220), "Mugs & Cups": (12, 45),
                       "Cookware": (30, 250), "Kitchen Tools": (8, 60), "Pantry & Treats": (6, 35),
                       "Gift Boxes": (35, 150), "Cookbooks": (18, 45)},
        "non_gift_examples": ["paper filter refill (200 pack)", "descaling solution", "replacement carafe lid"],
    },
    "general": {
        "count": 400, "about": "a general gift and lifestyle boutique (home, self-care, stationery, games, outdoors, tech accessories, snacks)",
        "categories": {"Home Decor": (15, 180), "Self-Care": (12, 90), "Stationery": (6, 45), "Games & Puzzles": (12, 60),
                       "Outdoor & Garden": (15, 160), "Tech Accessories": (15, 120), "Snacks & Treats": (8, 45),
                       "Kids": (10, 70), "Books": (12, 40), "Kitchen & Bar": (15, 140), "Wellness": (20, 150)},
        "non_gift_examples": ["gift card", "shipping protection", "replacement charging cable", "bulk shipping box"],
    },
}

SYSTEM = """You write realistic product listings for a Shopify store's catalog, used as test data.

Return JSON only: {"products": [{"title": str, "product_type": str, "vendor": str,
"description_html": str, "tags": [str], "price_min": number, "price_max": number}]}

- description_html: 50-110 words in <p> tags, like a real product page: what it is, materials,
  size or contents, how it's used. Concrete and specific; no invented awards or reviews.
- tags: 3-8 lowercase words a merchant would use (materials, scents, themes, "gift" where fitting).
- price_min/price_max in USD within the category's range; price_max > price_min only when there
  are variants (sizes, sets).
- Vary products: different materials, themes, price points and audiences. No two alike."""


def _prompt(store: str, spec: dict, n: int, existing: list[str], include_non_gift: bool) -> str:
    cats = "; ".join(f"{k} (${lo}-{hi})" for k, (lo, hi) in spec["categories"].items())
    lines = [
        f"Store: {spec['about']}.",
        f"Write {n} new products spread across these categories and price ranges: {cats}.",
    ]
    if include_non_gift:
        lines.append(f"Include 1-2 items that are poor gifts, like: {', '.join(spec['non_gift_examples'])}.")
    if existing:
        lines.append("Don't repeat any of these existing titles: " + "; ".join(existing[-80:]))
    return "\n".join(lines)


def _valid(item) -> bool:
    try:
        return (isinstance(item, dict) and item.get("title") and item.get("product_type")
                and float(item.get("price_min")) > 0)
    except (TypeError, ValueError):
        return False


async def generate_catalog(store: str, spec: dict, chat_fn: ChatFn = chat, ledger: Ledger | None = None,
                           count: int | None = None) -> list[dict]:
    target = count or spec["count"]
    products: list[dict] = []
    seen: set[str] = set()
    attempts = 0
    while len(products) < target and attempts < target // CHUNK * 3 + 5:
        attempts += 1
        n = min(CHUNK, target - len(products))
        if ledger:
            ledger.check(EST_COST_PER_CHUNK)
        resp = await chat_fn(
            model=GENERATOR_MODEL, system=SYSTEM,
            prompt=_prompt(store, spec, n, [p["title"] for p in products], include_non_gift=attempts % 4 == 1),
            max_tokens=8000, temperature=0.9, json_mode=True,
        )
        if ledger:
            ledger.charge("generate_catalogs", GENERATOR_MODEL, resp.input_tokens, resp.output_tokens)
        try:
            items = json.loads(resp.text).get("products") or []
        except (ValueError, AttributeError):
            continue
        for it in items:
            if not _valid(it) or it["title"].strip().lower() in seen:
                continue
            seen.add(it["title"].strip().lower())
            price_min = float(it["price_min"])
            products.append({
                "product_id": f"{store}-{len(products) + 1:04d}",
                "title": it["title"].strip(),
                "product_type": str(it.get("product_type", "")).strip(),
                "vendor": str(it.get("vendor", "")).strip(),
                "description": str(it.get("description_html", "")),
                "tags": [str(t).lower() for t in (it.get("tags") or [])][:10],
                "price_min": price_min,
                "price_max": max(price_min, float(it.get("price_max") or price_min)),
                "available": True,
            })
            if len(products) >= target:
                break
    return products
