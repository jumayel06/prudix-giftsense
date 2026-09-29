"""AI models: registry, merchant-facing AI tiers, slots and gradual rollout.

The one place model IDs live. Merchants never see a model name: they pick an
AI tier (Fast / Balanced / Premium) and each tier, like each background job,
is a slot that points at a model. Swapping a model is a config change here:

  1. add the model to MODELS (price, weight-relevant cost, capabilities)
  2. set it as the slot's "next" with a small rollout_pct (5 → 25 → 100)
     — each shop lands in a fixed bucket, so it stays on one model; set the
     pct back to 0 to roll back instantly
  3. at 100%, promote it to the slot's "model" and mark the old one retired
     with a "replacement"

Retired models always resolve to their replacement, and app/llm.py retries a
"model not found" error once on the model's fallback, so a provider retiring a
model before we update this file never reaches a merchant or shopper.
tests/unit/test_ai_models.py fails CI when a model in use is within 60 days of
its provider's earliest retirement date.
"""
import hashlib
from datetime import date

from app.config import PLAN_DEFAULT_AI_TIER, PLANS

# ── Registry ─────────────────────────────────────────────────────────────────
# price: USD per million tokens. caps drive request shaping in app/llm.py:
#   max_tokens_param   request field for the output cap
#   temperature        "always" | "never" (400s on non-default) | "thinking_off"
#   thinking_off       extra request fields that turn thinking/reasoning off
#   thinking_on        extra request fields when a caller opts into thinking
# provider_retires_on: provider's earliest possible retirement (None = none announced).
_OPENAI_REASONING = {"max_tokens_param": "max_completion_tokens", "temperature": "thinking_off",
                     "thinking_off": {"reasoning_effort": "none"}, "thinking_on": {"reasoning_effort": "medium"}}
_OPENAI_CLASSIC = {"max_tokens_param": "max_tokens", "temperature": "always", "thinking_off": {}, "thinking_on": {}}

MODELS = {
    "gpt-6-luna": {
        "provider": "openai", "price": (0.10, 0.50), "status": "active", "fallback": "gpt-6-sol",
        "caps": _OPENAI_REASONING, "provider_retires_on": None,
    },
    "gpt-6-sol": {
        "provider": "openai", "price": (2.00, 10.00), "status": "active", "fallback": "gpt-6-luna",
        "caps": _OPENAI_REASONING, "provider_retires_on": None,
    },
    "claude-sonnet-5": {
        "provider": "anthropic", "price": (2.00, 10.00), "status": "active", "fallback": "gpt-6-sol",
        "caps": {"max_tokens_param": "max_tokens", "temperature": "never",
                 "thinking_off": {"thinking": {"type": "disabled"}}, "thinking_on": {}},
        "provider_retires_on": date(2027, 6, 30),
    },
    # Evaluated 2026-09-28 against Sonnet 5 (same price, ~20% faster, no clear
    # quality gain); ready to roll out to ai_premium as "next".
    "claude-sonnet-5-5": {
        "provider": "anthropic", "price": (2.00, 10.00), "status": "active", "fallback": "claude-sonnet-5",
        # Rejects thinking "disabled" (400); "between_tools" without tools = no thinking.
        "caps": {"max_tokens_param": "max_tokens", "temperature": "never",
                 "thinking_off": {"thinking": {"type": "between_tools"}}, "thinking_on": {}},
        "provider_retires_on": date(2027, 9, 28),
    },
    # Retired 2026-09-28 by the six-model eval (kept for historical cost and
    # so old stored values resolve): GPT-6 Luna beat Haiku 4.5 on speed, reason
    # accuracy and cost, and GPT-6 replaced the GPT-4 generation.
    "claude-haiku-4-5": {
        "provider": "anthropic", "price": (1.00, 5.00), "status": "retired", "replacement": "gpt-6-luna",
        "caps": {"max_tokens_param": "max_tokens", "temperature": "always", "thinking_off": {}, "thinking_on": {}},
        "provider_retires_on": date(2026, 10, 15),
    },
    "gpt-4o-mini": {
        "provider": "openai", "price": (0.15, 0.60), "status": "retired", "replacement": "gpt-6-luna",
        "caps": _OPENAI_CLASSIC, "provider_retires_on": None,
    },
    "gpt-4.1": {
        "provider": "openai", "price": (2.00, 8.00), "status": "retired", "replacement": "gpt-6-sol",
        "caps": _OPENAI_CLASSIC, "provider_retires_on": None,
    },
}

# ── Slots: what each tier / background job runs on ──────────────────────────
SLOTS = {
    "ai_fast":          {"model": "gpt-6-luna",      "next": None, "rollout_pct": 0},
    "ai_balanced":      {"model": "gpt-6-sol",       "next": None, "rollout_pct": 0},
    "ai_premium":       {"model": "claude-sonnet-5", "next": None, "rollout_pct": 0},
    # Product gift profiles (was Haiku 4.5 until 2026-09-28; Luna ≈ 12× cheaper).
    "catalog_analysis": {"model": "gpt-6-luna",      "next": None, "rollout_pct": 0},
}

# ── AI tiers (what merchants see and pick) ───────────────────────────────────
# "weight" = generations per AI use: 1 / 2 / 4, each tier doubling the last.
# It belongs to the tier, not the model, so a model swap never changes what a
# merchant is charged (a new model must fit the tier's cost per generation).
AI_TIERS = {
    "fast":     {"label": "Fast",     "weight": 1, "slot": "ai_fast",
                 "description": "Quickest answers, and uses the fewest generations."},
    "balanced": {"label": "Balanced", "weight": 2, "slot": "ai_balanced",
                 "description": "The most carefully worded gift reasons."},
    "premium":  {"label": "Premium",  "weight": 4, "slot": "ai_premium",
                 "description": "Our strongest AI for gift picks and notes."},
}

# Values stored before AI tiers existed (model IDs) → their tier.
LEGACY_SELECTIONS = {
    "gpt-6-luna": "fast", "gpt-4o-mini": "fast", "claude-haiku-4-5": "fast",
    "gpt-6-sol": "balanced", "gpt-4.1": "balanced",
    "claude-sonnet-5": "premium", "claude-sonnet-5-5": "premium",
}

AI_TIER_WEIGHTS = {t: spec["weight"] for t, spec in AI_TIERS.items()}


def model_status(model: str | None) -> str:
    spec = MODELS.get(model or "")
    return spec["status"] if spec else "retired"


def _active(model: str | None) -> str | None:
    """Follow replacements from a retired model to an active one."""
    seen = set()
    while model is not None and model_status(model) != "active" and model not in seen:
        seen.add(model)
        model = (MODELS.get(model) or {}).get("replacement")
    return model if model_status(model) == "active" else None


def rollout_bucket(slot: str, shop_id) -> int:
    """0–99, fixed per shop and slot, so a shop stays on one side of a rollout."""
    digest = hashlib.sha256(f"{slot}:{shop_id}".encode()).hexdigest()
    return int(digest[:8], 16) % 100


def resolve_model(slot: str, shop_id=None, pins: dict | None = None) -> str:
    """The model a slot runs for this shop: an admin pin wins (unless retired),
    then the rollout bucket picks "next" or "model"; retired → replacement."""
    pinned = _active((pins or {}).get(slot))
    if pinned:
        return pinned
    spec = SLOTS[slot]
    if spec.get("next") and shop_id is not None and rollout_bucket(slot, shop_id) < spec.get("rollout_pct", 0):
        chosen = _active(spec["next"])
        if chosen:
            return chosen
    return _active(spec["model"]) or spec["model"]


def ai_tier_for(plan_tier: str | None, selected: str | None) -> str:
    """The shop's AI tier: its choice if the plan includes it, else the plan default."""
    plan = plan_tier if plan_tier in PLANS else "starter"
    tier = LEGACY_SELECTIONS.get(selected, selected)
    return tier if tier in PLANS[plan]["ai_tiers"] else PLAN_DEFAULT_AI_TIER[plan]


def model_for_shop(shop) -> str:
    """The model a shop's gift searches and notes run on right now."""
    tier = ai_tier_for(shop.plan_tier, shop.selected_model)
    return resolve_model(AI_TIERS[tier]["slot"], shop.id, getattr(shop, "model_pins", None))


def catalog_model_for(shop) -> str:
    return resolve_model("catalog_analysis", shop.id, getattr(shop, "model_pins", None))


def price_per_token(model: str) -> tuple[float, float]:
    """(input, output) USD per token; unknown models cost like the priciest
    active model so spend is never understated."""
    spec = MODELS.get(model)
    if spec is None:
        spec = max((m for m in MODELS.values() if m["status"] == "active"), key=lambda m: m["price"][1])
    return spec["price"][0] / 1e6, spec["price"][1] / 1e6
