"""Unit tests for core/config.py — Settings properties + env-driven app host."""

import pytest

from core.config import Settings


class TestSettingsIsProduction:

    def test_production_env_returns_true(self):
        s = Settings(app_env="production")
        assert s.is_production is True

    def test_development_env_returns_false(self):
        s = Settings(app_env="development")
        assert s.is_production is False

    def test_test_env_returns_false(self):
        s = Settings(app_env="test")
        assert s.is_production is False

    def test_empty_env_returns_false(self):
        s = Settings(app_env="")
        assert s.is_production is False


class TestGetAppHost:

    def test_returns_app_host_when_set(self):
        s = Settings(app_host="myapp.railway.app")
        assert s.get_app_host() == "myapp.railway.app"

    def test_empty_when_unset(self):
        # APP_HOST is env-driven only (Railway in prod, .env locally) — no
        # baked-in default/fallback. An empty value surfaces the misconfig
        # loudly instead of silently emitting the wrong host (2026-09-11 bug).
        s = Settings(app_host="")
        assert s.get_app_host() == ""


def test_plan_models_after_2026_09_28_eval():
    from app.config import MODEL_WEIGHTS, PLAN_DEFAULT_MODELS, PLANS
    assert PLANS["starter"]["models_available"] == ["gpt-6-luna", "claude-haiku-4-5"]
    assert PLANS["growth"]["models_available"] == ["gpt-6-luna", "claude-haiku-4-5", "gpt-6-sol"]
    assert PLANS["pro"]["models_available"] == ["gpt-6-luna", "claude-haiku-4-5", "gpt-6-sol", "claude-sonnet-5"]
    assert PLAN_DEFAULT_MODELS == {"starter": "gpt-6-luna", "growth": "gpt-6-sol", "pro": "claude-sonnet-5"}
    assert MODEL_WEIGHTS == {"gpt-6-luna": 1, "claude-haiku-4-5": 2, "gpt-6-sol": 4, "claude-sonnet-5": 4}
    # Every offered model has a weight and a price.
    from app.llm import MODEL_COSTS
    for plan in PLANS.values():
        for m in plan["models_available"]:
            assert m in MODEL_WEIGHTS and m in MODEL_COSTS


@pytest.mark.parametrize("tier,selected,expected", [
    ("growth", "claude-haiku-4-5", "claude-haiku-4-5"),   # allowed choice kept
    ("starter", "claude-sonnet-5", "gpt-6-luna"),          # not in plan → plan default
    ("starter", "gpt-4o-mini", "gpt-6-luna"),              # retired → replacement
    ("growth", "gpt-4.1", "gpt-6-sol"),
    ("starter", "gpt-4.1", "gpt-6-luna"),                  # replacement not in plan → default
    ("none", None, "gpt-6-luna"),                          # no plan → starter default
])
def test_effective_model(tier, selected, expected):
    from app.config import effective_model
    assert effective_model(tier, selected) == expected
