"""GET /api/stats — the dashboard's boot call (ported from Prudix Commerce).

The dashboard reads `plan_status` from here on load and routes `pending` shops
to the plan picker, so this is what makes a fresh install land on /plans.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import PLANS
from app.main import app
from core.db.session import get_db
from tests.conftest import TEST_SHOP_DOMAIN, add_usage, make_shop


def _client(db_session):
    async def override_db():
        yield db_session
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _get(db_session):
    for client in _client(db_session):
        resp = client.get(f"/api/stats?shop={TEST_SHOP_DOMAIN}")
    assert resp.status_code == 200
    return resp.json()


@pytest.mark.asyncio
async def test_pending_shop_reports_pending_for_plan_picker_routing(db_session):
    db_session.add(make_shop(plan_tier="none", plan_status="pending", billing_cycle_start=None))
    await db_session.commit()

    data = _get(db_session)
    assert data["plan_status"] == "pending"
    assert data["generations_used"] == 0


@pytest.mark.asyncio
async def test_active_shop_usage_is_sum_within_current_cycle(db_session):
    cycle = datetime.now(timezone.utc) - timedelta(days=5)
    shop = make_shop(plan_tier="growth", plan_status="active", billing_cycle_start=cycle)
    db_session.add(shop)
    await db_session.commit()
    await add_usage(db_session, shop, generations=4)                       # this cycle
    await add_usage(db_session, shop, generations=2)                       # this cycle
    await add_usage(db_session, shop, generations=-2)                      # refund
    await add_usage(db_session, shop, generations=100, created_at=cycle - timedelta(days=1))  # last cycle

    data = _get(db_session)
    limit = PLANS["growth"]["generation_limit"]
    assert data["generations_used"] == 4
    assert data["generation_limit"] == limit
    assert data["usage_pct"] == round(4 / limit * 100, 1)
    assert data["plan_name"] == "Growth"
    assert data["days_remaining"] == 25
    assert data["ai_tier"] == "standard" and data["ai_tier_label"] == "Standard"
    assert data["ai_tier_weight"] == 1 and data["ai_model_label"] == "GPT-6 Luna"


@pytest.mark.asyncio
async def test_trial_shop_reports_trial_cap_and_days_left(db_session):
    now = datetime.now(timezone.utc)
    shop = make_shop(
        plan_tier="pro", plan_status="trial_active", billing_cycle_start=now - timedelta(days=2),
        trial_used=True, trial_ends_at=now + timedelta(days=5, hours=1),
    )
    db_session.add(shop)
    await db_session.commit()
    await add_usage(db_session, shop, generations=8)

    data = _get(db_session)
    assert data["trial_generations_cap"] == PLANS["pro"]["trial_generations"]
    assert data["trial_generations_used"] == 8
    assert data["trial_days_remaining"] == 6
    assert data["trial_price_after"] == PLANS["pro"]["price_usd"]


@pytest.mark.asyncio
async def test_cancelled_shop_in_paid_period_reports_access_until(db_session):
    cycle = datetime.now(timezone.utc) - timedelta(days=10)
    db_session.add(make_shop(plan_tier="growth", plan_status="cancelled", billing_cycle_start=cycle))
    await db_session.commit()

    data = _get(db_session)
    assert datetime.fromisoformat(data["access_until"]) == cycle + timedelta(days=30)


@pytest.mark.asyncio
async def test_scheduled_downgrade_is_reported(db_session):
    when = datetime.now(timezone.utc) + timedelta(days=12)
    shop = make_shop(plan_tier="pro", plan_status="active")
    shop.scheduled_plan_tier = "starter"
    shop.scheduled_change_at = when
    db_session.add(shop)
    await db_session.commit()

    data = _get(db_session)
    assert data["scheduled_plan_tier"] == "starter"
    assert data["scheduled_plan_name"] == "Starter"


@pytest.mark.asyncio
async def test_review_prompt_hidden_without_listing_slug(db_session, monkeypatch):
    from core import config as core_config
    monkeypatch.setattr(core_config.settings, "app_store_listing_slug", "")
    shop = make_shop(plan_status="active")
    db_session.add(shop)
    await db_session.commit()
    await add_usage(db_session, shop, generations=2)

    data = _get(db_session)
    assert data["show_review_prompt"] is False
    await db_session.refresh(shop)
    assert shop.review_prompt_shown is False  # flag not burned before the listing exists


@pytest.mark.asyncio
async def test_review_prompt_shown_exactly_once(db_session, monkeypatch):
    from core import config as core_config
    monkeypatch.setattr(core_config.settings, "app_store_listing_slug", "prudix-giftsense")
    shop = make_shop(plan_status="active")
    db_session.add(shop)
    await db_session.commit()
    await add_usage(db_session, shop, generations=2)

    # Other screens' /api/stats calls (App shell, Plans) don't consume it…
    first = _get(db_session)
    second = _get(db_session)
    assert first["show_review_prompt"] is True and second["show_review_prompt"] is True
    assert "apps.shopify.com/prudix-giftsense" in first["review_prompt_url"]
    await db_session.refresh(shop)
    assert shop.review_prompt_shown is False

    # …only Home reporting the banner as seen does, exactly once.
    from app.routes.stats import mark_review_prompt_seen
    assert await mark_review_prompt_seen(shop, db_session) == {"ok": True, "first": True}
    assert await mark_review_prompt_seen(shop, db_session) == {"ok": True, "first": False}
    await db_session.refresh(shop)
    assert shop.review_prompt_shown is True
    after = _get(db_session)
    assert after["show_review_prompt"] is False and after["review_prompt_url"] is None
