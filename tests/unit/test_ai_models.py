"""app/ai_models.py: registry, AI tiers, slots, gradual rollout, retirement."""
import uuid
from datetime import date, timedelta

import pytest

from app import ai_models as am
from app.config import PLAN_DEFAULT_AI_TIER, PLANS

RETIREMENT_WARNING_DAYS = 60


# ── CI tripwire ──────────────────────────────────────────────────────────────

def test_no_model_in_use_is_near_its_retirement():
    """Fails CI when a model any slot can run is within 60 days of its
    provider's earliest retirement date: time to roll out a replacement."""
    horizon = date.today() + timedelta(days=RETIREMENT_WARNING_DAYS)
    in_use = {m for s in am.SLOTS.values() for m in (s["model"], s.get("next")) if m}
    in_use |= {am.MODELS[m]["fallback"] for m in in_use if am.MODELS[m].get("fallback")}
    for m in in_use:
        assert am.model_status(m) == "active", f"{m} is used by a slot but not active"
        retires = am.MODELS[m].get("provider_retires_on")
        assert retires is None or retires > horizon, (
            f"{m} may be retired by its provider on {retires}: add a replacement to the slot's "
            f"'next' and roll it out (see app/ai_models.py)")


# ── Registry / plans consistency ─────────────────────────────────────────────

def test_registry_and_plans_are_consistent():
    for spec in am.MODELS.values():
        if spec["status"] != "active":
            assert am.model_status(spec.get("replacement")) == "active"
        assert {"max_tokens_param", "temperature", "thinking_off", "thinking_on"} <= spec["caps"].keys()
    for slot in am.SLOTS.values():
        assert slot["model"] in am.MODELS and am.model_status(slot["model"]) == "active"
    for tier in am.AI_TIERS.values():
        assert tier["slot"] in am.SLOTS
    for plan_tier, plan in PLANS.items():
        assert set(plan["ai_tiers"]) <= set(am.AI_TIERS)
        assert PLAN_DEFAULT_AI_TIER[plan_tier] in plan["ai_tiers"]


def test_current_lineup():
    assert PLANS["starter"]["ai_tiers"] == ["standard"]
    assert PLANS["growth"]["ai_tiers"] == ["standard", "advanced"]
    assert PLANS["pro"]["ai_tiers"] == ["standard", "advanced", "premium"]
    assert am.AI_TIER_WEIGHTS == {"standard": 1, "advanced": 2, "premium": 4}
    assert {t: am.SLOTS[s["slot"]]["model"] for t, s in am.AI_TIERS.items()} == {
        "standard": "gpt-6-luna", "advanced": "gpt-6-sol", "premium": "claude-sonnet-5"}
    assert am.SLOTS["catalog_analysis"]["model"] == "gpt-6-luna"
    assert am.model_status("claude-haiku-4-5") == "retired"


# ── Tiers ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("plan,selected,expected", [
    ("pro", "premium", "premium"),
    ("starter", "premium", "standard"),          # not in plan → plan default
    ("growth", None, "advanced"),
    ("none", None, "standard"),                  # no plan → starter default
    ("pro", "claude-sonnet-5", "premium"),   # stored before tiers existed
    ("pro", "claude-haiku-4-5", "standard"),
    ("growth", "gpt-4.1", "advanced"),
    ("pro", "fast", "standard"),              # first tier names (renamed 2026-09-28)
    ("growth", "balanced", "advanced"),
])
def test_ai_tier_for(plan, selected, expected):
    assert am.ai_tier_for(plan, selected) == expected


# ── Slots, rollout, pins, retirement ─────────────────────────────────────────

@pytest.fixture
def rollout(monkeypatch):
    slots = {**am.SLOTS, "ai_premium": {"model": "claude-sonnet-5", "next": "claude-sonnet-5-5", "rollout_pct": 25}}
    monkeypatch.setattr(am, "SLOTS", slots)
    return slots


def test_rollout_is_sticky_per_shop_and_near_the_percentage(rollout):
    shops = [uuid.uuid4() for _ in range(2000)]
    on_next = [s for s in shops if am.resolve_model("ai_premium", s) == "claude-sonnet-5-5"]
    assert 0.20 < len(on_next) / len(shops) < 0.30
    assert all(am.resolve_model("ai_premium", s) == "claude-sonnet-5-5" for s in on_next)  # same answer again


def test_rollout_zero_is_an_instant_rollback(rollout):
    rollout["ai_premium"]["rollout_pct"] = 0
    assert {am.resolve_model("ai_premium", uuid.uuid4()) for _ in range(200)} == {"claude-sonnet-5"}


def test_pin_holds_a_shop_on_a_model(rollout):
    shop = next(s for s in (uuid.uuid4() for _ in range(1000)) if am.rollout_bucket("ai_premium", s) < 25)
    assert am.resolve_model("ai_premium", shop) == "claude-sonnet-5-5"
    assert am.resolve_model("ai_premium", shop, pins={"ai_premium": "claude-sonnet-5"}) == "claude-sonnet-5"


def test_retired_models_resolve_to_their_replacement(monkeypatch):
    monkeypatch.setattr(am, "SLOTS", {**am.SLOTS, "ai_standard": {"model": "claude-haiku-4-5", "next": None,
                                                              "rollout_pct": 0}})
    assert am.resolve_model("ai_standard", uuid.uuid4()) == "gpt-6-luna"
    assert am.resolve_model("ai_premium", uuid.uuid4(), pins={"ai_premium": "gpt-4.1"}) == "gpt-6-sol"


def test_model_for_shop_uses_its_ai_tier():
    class S:
        id = uuid.uuid4(); plan_tier = "pro"; selected_model = "advanced"; model_pins = None  # noqa: E702
    assert am.model_for_shop(S) == "gpt-6-sol"


def test_unknown_model_is_priced_like_the_priciest_active_model():
    assert am.price_per_token("mystery-model") == am.price_per_token("claude-sonnet-5")


def test_tier_models_for_shop_follows_rollout_and_pins(rollout):
    class S:
        plan_tier = "pro"; model_pins = None  # noqa: E702
    S.id = next(s for s in (uuid.uuid4() for _ in range(1000)) if am.rollout_bucket("ai_premium", s) < 25)
    assert am.tier_models_for_shop(S) == {"standard": "GPT-6 Luna", "advanced": "GPT-6 Sol", "premium": "Claude Sonnet 5.5"}
    S.model_pins = {"ai_premium": "claude-sonnet-5"}
    assert am.tier_models_for_shop(S)["premium"] == "Claude Sonnet 5"


def test_every_model_has_a_display_label():
    assert all(spec.get("label") for spec in am.MODELS.values())
