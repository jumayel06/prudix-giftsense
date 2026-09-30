"""Storefront API behind the App Proxy (/apps/giftsense/* → /api/storefront/*).

Only requests Shopify signed are trusted: the shop comes from the signed
`shop` param, never from the body. Shoppers always get picks; limits and
AI failures only change the reasons (templates), never break the widget."""
import hashlib
import hmac
import time
import uuid
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import PLANS
from app.main import app
from app.services.gifting.embeddings import FakeEmbedder
from app.services.proxy_auth import verify_proxy_signature
from core.db.models import UsageLog
from core.db.session import get_db
from tests.conftest import TEST_API_SECRET, TEST_SHOP_DOMAIN, add_usage, make_shop
from tests.integration.test_playground import picking_chat, row

SID = str(uuid.uuid4())
BRIEF = {"sid": SID, "recipient": "friend", "occasion": "birthday", "budget_band": "25_50", "vibes": ["cozy"]}


def signed(params: dict, secret: str = TEST_API_SECRET) -> dict:
    """Sign like Shopify's App Proxy: sorted k=v pairs joined with no separator."""
    message = "".join(f"{k}={v}" for k, v in sorted(params.items()))
    return {**params, "signature": hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()}


def proxy_params(shop=TEST_SHOP_DOMAIN, **extra):
    return signed({"shop": shop, "path_prefix": "/apps/giftsense", "timestamp": str(int(time.time())),
                   "logged_in_customer_id": "", **extra})


def call(db_session, method, path, params=None, **kw):
    async def override_db():
        yield db_session
    app.dependency_overrides[get_db] = override_db
    try:
        qs = urlencode(params if params is not None else proxy_params())
        return TestClient(app).request(method, f"{path}?{qs}", **kw)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def models():
    with patch("app.routes.storefront.chat", picking_chat()) as chat, \
         patch("app.routes.storefront.OpenAIEmbedder", return_value=FakeEmbedder()):
        yield chat


async def seeded_shop(db, n=4, **kw):
    shop = make_shop(**kw)
    db.add(shop)
    await db.flush()
    db.add_all([row(shop, str(i)) for i in range(n)])
    await db.commit()
    return shop


# ── Signature ────────────────────────────────────────────────────────────────

def test_valid_signature_passes_and_tampering_fails():
    p = proxy_params()
    assert verify_proxy_signature(p)
    assert not verify_proxy_signature({**p, "shop": "evil.myshopify.com"})
    assert not verify_proxy_signature({k: v for k, v in p.items() if k != "signature"})
    assert not verify_proxy_signature(signed({"shop": TEST_SHOP_DOMAIN, "timestamp": str(int(time.time()))},
                                             secret="wrong"))


def test_stale_or_missing_timestamp_is_rejected():
    old = signed({"shop": TEST_SHOP_DOMAIN, "timestamp": str(int(time.time()) - 3600)})
    assert not verify_proxy_signature(old)
    assert not verify_proxy_signature(signed({"shop": TEST_SHOP_DOMAIN}))


def test_repeated_params_are_joined_with_commas_like_shopify():
    ts = str(int(time.time()))
    message = f"extra=1,2shop={TEST_SHOP_DOMAIN}timestamp={ts}"
    sig = hmac.new(TEST_API_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    params = [("shop", TEST_SHOP_DOMAIN), ("timestamp", ts), ("extra", "1"), ("extra", "2"), ("signature", sig)]
    assert verify_proxy_signature(params)


@pytest.mark.asyncio
async def test_unsigned_request_is_rejected(db_session):
    await seeded_shop(db_session)
    resp = call(db_session, "GET", "/api/storefront/config", params={"shop": TEST_SHOP_DOMAIN})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_unknown_or_uninstalled_shop_is_404(db_session):
    await seeded_shop(db_session, plan_status="uninstalled")
    assert call(db_session, "GET", "/api/storefront/config").status_code == 404
    assert call(db_session, "GET", "/api/storefront/config",
                params=proxy_params(shop="other.myshopify.com")).status_code == 404


# ── Config ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_config_gives_the_widget_its_intake_and_branding(db_session):
    await seeded_shop(db_session, plan_tier="starter")
    data = call(db_session, "GET", "/api/storefront/config").json()
    assert data["enabled"] is True and data["show_badge"] is True
    assert {"value": "friend", "label": "Friend"} in data["intake"]["recipients"]


@pytest.mark.asyncio
async def test_growth_can_hide_the_badge(db_session):
    await seeded_shop(db_session, plan_tier="growth")
    assert call(db_session, "GET", "/api/storefront/config").json()["show_badge"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "expired", "declined"])
async def test_inactive_plan_disables_the_widget(db_session, status):
    await seeded_shop(db_session, plan_status=status)
    assert call(db_session, "GET", "/api/storefront/config").json() == {"enabled": False}
    assert call(db_session, "POST", "/api/storefront/search", json=BRIEF).status_code == 403


# ── Search ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_returns_picks_and_meters(db_session, models):
    shop = await seeded_shop(db_session, plan_tier="growth", selected_model="advanced")
    resp = call(db_session, "POST", "/api/storefront/search", json=BRIEF)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["picks"]) == 3
    p = data["picks"][0]
    assert {"product_id", "title", "url", "image_url", "price_min", "price_max", "reason"} <= p.keys()
    assert p["reason"] == "Soy wax for slow evenings."
    # Shoppers never see metering or model details.
    assert "limited" not in data and "model" not in str(data).lower()
    used = (await db_session.execute(select(func.sum(UsageLog.generations_consumed))
                                     .where(UsageLog.shop_id == shop.id))).scalar()
    assert used == 2


@pytest.mark.asyncio
async def test_over_limit_still_returns_picks_without_ai(db_session, models):
    shop = await seeded_shop(db_session, plan_tier="starter", selected_model="standard")
    await add_usage(db_session, shop, PLANS["starter"]["generation_limit"])
    data = call(db_session, "POST", "/api/storefront/search", json=BRIEF).json()
    assert len(data["picks"]) >= 3 and all(p["reason"] for p in data["picks"])
    models.assert_not_awaited()


@pytest.mark.asyncio
async def test_body_cannot_choose_the_shop(db_session, models):
    other = make_shop(domain="victim.myshopify.com")
    db_session.add(other)
    await seeded_shop(db_session)
    call(db_session, "POST", "/api/storefront/search", json={**BRIEF, "shop": "victim.myshopify.com"})
    assert not (await db_session.execute(select(UsageLog).where(UsageLog.shop_id == other.id))).scalars().all()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{"sid": "not-a-uuid"}, {"recipient": "alien"}, {"free_text": "x" * 201},
                                 {"exclude_ids": ["1"] * 51}])
async def test_search_validates_input(db_session, models, bad):
    await seeded_shop(db_session)
    assert call(db_session, "POST", "/api/storefront/search", json={**BRIEF, **bad}).status_code == 422


@pytest.mark.asyncio
async def test_exclude_ids_skip_products_already_shown(db_session, models):
    await seeded_shop(db_session, n=6)
    shown = ["0", "1", "2"]
    data = call(db_session, "POST", "/api/storefront/search", json={**BRIEF, "exclude_ids": shown}).json()
    assert not {p["product_id"] for p in data["picks"]} & set(shown)


# ── Abuse limits ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_session_limit_returns_429_after_10_searches(db_session, models):
    await seeded_shop(db_session)
    codes = [call(db_session, "POST", "/api/storefront/search", json=BRIEF).status_code for _ in range(11)]
    assert codes[:10] == [200] * 10 and codes[10] == 429


@pytest.mark.asyncio
async def test_ip_limit_applies_across_sessions(db_session, models):
    await seeded_shop(db_session)
    ip = {"X-Forwarded-For": "203.0.113.9, 162.158.1.1"}
    codes = [call(db_session, "POST", "/api/storefront/search", json={**BRIEF, "sid": str(uuid.uuid4())},
                  headers=ip).status_code for _ in range(31)]
    assert codes.count(200) == 30 and codes[-1] == 429
    other = {"X-Forwarded-For": "198.51.100.7"}
    assert call(db_session, "POST", "/api/storefront/search", json=BRIEF, headers=other).status_code == 200


# ── Instant phase (picks at once; AI reasons follow in a parallel request) ──

@pytest.mark.asyncio
async def test_instant_phase_returns_picks_without_ai_or_charges(db_session, models):
    shop = await seeded_shop(db_session, plan_tier="pro", selected_model="premium")
    data = call(db_session, "POST", "/api/storefront/search", json={**BRIEF, "phase": "instant"}).json()
    assert data["phase"] == "instant" and len(data["picks"]) >= 3 and all(p["reason"] for p in data["picks"])
    models.assert_not_awaited()
    assert not (await db_session.execute(select(UsageLog).where(UsageLog.shop_id == shop.id))).scalars().all()


@pytest.mark.asyncio
async def test_default_phase_is_the_metered_ai_search(db_session, models):
    await seeded_shop(db_session)
    data = call(db_session, "POST", "/api/storefront/search", json=BRIEF).json()
    assert data["phase"] == "ai" and models.await_count == 1


@pytest.mark.asyncio
async def test_instant_and_ai_calls_have_separate_session_limits(db_session, models):
    # One search = one instant + one AI request; the pair must not count twice.
    await seeded_shop(db_session)
    for _ in range(10):
        assert call(db_session, "POST", "/api/storefront/search", json={**BRIEF, "phase": "instant"}).status_code == 200
        assert call(db_session, "POST", "/api/storefront/search", json=BRIEF).status_code == 200
    assert call(db_session, "POST", "/api/storefront/search", json=BRIEF).status_code == 429


@pytest.mark.asyncio
async def test_unknown_phase_is_rejected(db_session, models):
    await seeded_shop(db_session)
    assert call(db_session, "POST", "/api/storefront/search", json={**BRIEF, "phase": "later"}).status_code == 422


# ── Gift sessions (one row per widget session: last brief, final picks) ──────

@pytest.mark.asyncio
async def test_ai_search_records_the_gift_session(db_session, models):
    from core.db.models import GiftSession
    shop = await seeded_shop(db_session, n=6)
    call(db_session, "POST", "/api/storefront/search", json=BRIEF)
    call(db_session, "POST", "/api/storefront/search", json={**BRIEF, "vibes": ["funny"], "exclude_ids": ["0"]})
    rows = (await db_session.execute(select(GiftSession).where(GiftSession.shop_id == shop.id))).scalars().all()
    assert len(rows) == 1
    s = rows[0]
    assert str(s.sid) == SID and s.searches == 2
    assert s.intake["vibes"] == ["funny"] and s.intake["recipient"] == "friend"
    assert len(s.last_picks) == 3


@pytest.mark.asyncio
async def test_instant_phase_does_not_record_a_session(db_session, models):
    from core.db.models import GiftSession
    await seeded_shop(db_session)
    call(db_session, "POST", "/api/storefront/search", json={**BRIEF, "phase": "instant"})
    assert (await db_session.execute(select(GiftSession))).scalars().all() == []


@pytest.mark.asyncio
async def test_sessions_are_per_shop(db_session, models):
    from core.db.models import GiftSession
    other = make_shop(domain="other.myshopify.com")
    db_session.add(other)
    await db_session.flush()
    db_session.add(GiftSession(shop_id=other.id, sid=uuid.UUID(SID), intake={}, last_picks=[], searches=5))
    await db_session.commit()
    shop = await seeded_shop(db_session)
    call(db_session, "POST", "/api/storefront/search", json=BRIEF)
    mine = (await db_session.execute(select(GiftSession).where(GiftSession.shop_id == shop.id))).scalar_one()
    assert mine.searches == 1


# ── Refine ("Not quite right?") ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_config_includes_refine_questions(db_session):
    await seeded_shop(db_session)
    q = call(db_session, "GET", "/api/storefront/config").json()["intake"]["refine_questions"]
    assert len(q) >= 3 and all(len(x["options"]) == 2 for x in q)
    from app.services.gifting import vocab
    assert all(o["vibe"] in vocab.VIBES for x in q for o in x["options"])


@pytest.mark.asyncio
async def test_refine_is_counted_and_limited_to_two_per_session(db_session, models):
    from core.db.models import GiftSession
    shop = await seeded_shop(db_session, n=8)
    assert call(db_session, "POST", "/api/storefront/search", json=BRIEF).status_code == 200
    for _ in range(2):
        assert call(db_session, "POST", "/api/storefront/search", json={**BRIEF, "refine": True}).status_code == 200
    resp = call(db_session, "POST", "/api/storefront/search", json={**BRIEF, "refine": True})
    assert resp.status_code == 409
    s = (await db_session.execute(select(GiftSession).where(GiftSession.shop_id == shop.id))).scalar_one()
    assert s.refines == 2 and s.searches == 3


@pytest.mark.asyncio
async def test_refine_without_a_prior_search_is_rejected(db_session, models):
    await seeded_shop(db_session)
    assert call(db_session, "POST", "/api/storefront/search", json={**BRIEF, "refine": True}).status_code == 409


@pytest.mark.asyncio
async def test_instant_refine_is_allowed_but_not_counted(db_session, models):
    from core.db.models import GiftSession
    await seeded_shop(db_session)
    call(db_session, "POST", "/api/storefront/search", json=BRIEF)
    assert call(db_session, "POST", "/api/storefront/search",
                json={**BRIEF, "refine": True, "phase": "instant"}).status_code == 200
    assert (await db_session.execute(select(GiftSession))).scalar_one().refines == 0
