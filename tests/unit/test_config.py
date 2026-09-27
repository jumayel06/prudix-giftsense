"""Unit tests for core/config.py — Settings properties + env-driven app host."""

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
