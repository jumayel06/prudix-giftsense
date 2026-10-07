"""Full analytics (Growth+) and the weekly email (app/services/analytics.py,
app/services/digest_email.py, app/workers/digest.py)."""
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.services import digest_email
from core.db.models import GiftEvent, GiftMedia, GiftOrder, GiftSession, OrderCountDaily, Shop
from tests.conftest import make_shop
def dashboard_get(db, path):
    base, _, query = path.partition("?")
    from fastapi.testclient import TestClient
    from app.main import app
    from core.db.session import get_db
    from tests.conftest import TEST_SHOP_DOMAIN

    async def override_db():
        yield db
    app.dependency_overrides[get_db] = override_db
    try:
        return TestClient(app).get(f"{base}?shop={TEST_SHOP_DOMAIN}{'&' + query if query else ''}")
    finally:
        app.dependency_overrides.clear()

NOW = datetime.now(timezone.utc)


async def activity(db, plan="growth", **shop_kw):
    shop = make_shop(plan_tier=plan, **shop_kw)
    db.add(shop)
    await db.flush()
    sids = [uuid.uuid4() for _ in range(3)]
    for s in sids:
        db.add(GiftEvent(shop_id=shop.id, sid=s, type="widget_open"))
        db.add(GiftSession(shop_id=shop.id, sid=s, searches=1, last_picks=[],
                           intake={"occasion": "birthday", "budget_band": "25_50", "recipient": "parent"}))
    db.add_all([
        GiftOrder(shop_id=shop.id, order_id="1", order_name="#1", sid=sids[0], gift_lines=2, gift_revenue=60,
                  order_total=60, currency="USD", delivery_mode="self", note_source="ai_accepted",
                  groups=[{"id": "g1", "label": "Mom", "wrap": "Gold", "message": f"m1-{shop.id}"}, {"id": "g2", "label": "Dad"}],
                  arrive_by=date.today() + timedelta(days=9)),
        GiftOrder(shop_id=shop.id, order_id="2", order_name="#2", sid=None, gift_lines=1, gift_revenue=40,
                  order_total=40, currency="USD", delivery_mode="direct", groups=[],
                  registry_id=uuid.uuid4(), registry_revenue=40),
        # previous 7 days
        GiftOrder(shop_id=shop.id, order_id="0", order_name="#0", sid=None, gift_lines=1, gift_revenue=20,
                  order_total=20, currency="USD", groups=[], created_at=NOW - timedelta(days=10)),
        GiftMedia(shop_id=shop.id, token=f"m1-{shop.id}", view_token=f"v1-{shop.id}", kind="video", storage_key="k", mime="video/mp4",
                  status="linked", order_id="1"),
        OrderCountDaily(shop_id=shop.id, day=date.today(), orders=8, revenue=500),
    ])
    await db.commit()
    return shop


# ── Dashboard ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_analytics_breakdowns_and_comparison(db_session):
    await activity(db_session)
    data = dashboard_get(db_session, "/api/analytics?days=7")
    data = data.json()
    assert data["full"] is True and data["days"] == 7 and len(data["daily"]) == 7
    assert data["gift_orders"] == 2 and data["previous"]["gift_orders"] == 1 and data["changes"]["gift_orders"] == 1.0
    assert data["wrap_attach_rate"] == pytest.approx(0.5) and data["message_attach_rate"] == pytest.approx(0.5)
    assert data["messages"] == {"voice": 0, "video": 1} and data["arrive_by_rate"] == pytest.approx(0.5)
    assert data["delivery_mix"] == {"direct": 1, "self": 1} and data["gifts_per_order"] == 2
    assert data["top_recipients"][0]["value"] == "parent"
    assert data["registry_orders"] == 1 and data["registry_revenue"] == 40

    month = dashboard_get(db_session, "/api/analytics?days=30").json()
    assert month["gift_orders"] == 3


@pytest.mark.asyncio
async def test_starter_gets_basic_30_days_only(db_session):
    await activity(db_session, plan="starter")
    data = dashboard_get(db_session, "/api/analytics?days=90").json()
    assert data["full"] is False and data["days"] == 30
    assert "wrap_attach_rate" not in data and "previous" not in data


@pytest.mark.asyncio
async def test_bad_range_is_rejected(db_session):
    await activity(db_session)
    assert dashboard_get(db_session, "/api/analytics?days=12").status_code == 422


# ── Weekly email ────────────────────────────────────────────────────────────

def test_email_renders_numbers_and_changes():
    week = {"sessions": 30, "gift_orders": 4, "attributed_revenue": 260.0, "conversion_rate": 0.2,
            "note_acceptance_rate": 0.5, "all_orders": 20, "currency": "USD",
            "top_occasions": [{"value": "birthday", "label": "Birthday", "count": 5}]}
    before = {"sessions": 20, "gift_orders": 4, "attributed_revenue": 0.0, "conversion_rate": 0.25,
              "note_acceptance_rate": None, "all_orders": 10, "currency": "USD", "top_occasions": []}
    with patch.object(__import__("app.services.email_style", fromlist=["x"]).core_settings, "shopify_app_handle", "giftsense-dev"):
        subject, html, text = digest_email.render("snow-co", "snow-co.myshopify.com", week, before)
    assert subject == "Your GiftSense week: 4 gift orders, $260 from the gift finder"
    assert "▲ 50%" in html and "▼ 5 pts" in html and "new" in html          # sessions, conversion, revenue from 0
    assert "20% of your 20 orders were gifts." in html and "Birthday" in html
    assert "https://admin.shopify.com/store/snow-co/apps/giftsense-dev/analytics" in html
    assert "Gift orders: 4" in text and "/settings" in text


@pytest.mark.asyncio
async def test_weekly_digest_sends_once_to_opted_in_growth_shops(db_session):
    from app.workers.digest import send_weekly_digests
    shop = await activity(db_session)
    shop.shop_owner_email = "owner@snow.co"
    starter = make_shop(domain="starter.myshopify.com", plan_tier="starter")
    starter.shop_owner_email = "s@x.co"
    quiet = make_shop(domain="quiet.myshopify.com", plan_tier="pro")
    quiet.shop_owner_email = "q@x.co"
    opted_out = make_shop(domain="out.myshopify.com", plan_tier="pro")
    opted_out.digest_email_opt_in = False
    opted_out.shop_owner_email = "o@x.co"
    db_session.add_all([starter, quiet, opted_out])
    await db_session.commit()

    send = AsyncMock(return_value={"message_id": "x"})
    with patch("app.workers.digest.AsyncSessionLocal") as sess, patch("app.workers.digest.send_email", send):
        sess.return_value.__aenter__.return_value = db_session
        sess.return_value.__aexit__.return_value = False
        first = await send_weekly_digests({}, NOW)
        again = await send_weekly_digests({}, NOW + timedelta(hours=2))      # worker restart: no double send
    assert first == {"sent": 1, "skipped": 1, "failed": 0}                     # quiet shop: no activity → skipped
    assert again["sent"] == 0
    assert send.await_count == 1 and send.await_args.kwargs["to_email"] == "owner@snow.co"
    await db_session.refresh(shop)
    assert shop.digest_last_sent_at is not None


@pytest.mark.asyncio
async def test_one_failing_shop_doesnt_stop_the_rest(db_session):
    from app.services.postmark_client import PostmarkError
    from app.workers.digest import send_weekly_digests
    a = await activity(db_session)
    a.shop_owner_email = "a@x.co"
    await db_session.commit()
    b = await activity(db_session, domain="b.myshopify.com")
    b.shop_owner_email = "b@x.co"
    await db_session.commit()
    send = AsyncMock(side_effect=[PostmarkError("down"), {"message_id": "x"}])
    with patch("app.workers.digest.AsyncSessionLocal") as sess, patch("app.workers.digest.send_email", send):
        sess.return_value.__aenter__.return_value = db_session
        sess.return_value.__aexit__.return_value = False
        result = await send_weekly_digests({}, NOW)
    assert result == {"sent": 1, "skipped": 0, "failed": 1}


def test_email_uses_the_prudix_shell_with_chart_and_tips():
    week = {"sessions": 3, "gift_orders": 1, "attributed_revenue": 40.0, "conversion_rate": 0.33,
            "note_acceptance_rate": None, "all_orders": 5, "currency": "USD", "top_occasions": [],
            "daily": [{"date": "2026-10-05", "gift_orders": 1, "gift_revenue": 40}]}
    _, html, text = digest_email.render("snow", "snow.myshopify.com", week, {"gift_orders": 0},
                                        tips=[("Offer gift wrap", "It takes a minute.", "wrap")])
    assert "Weekly report" in html and "Prudi" in html and "Gift orders per day" in html and re.search(r">\s*Mon\s*<", html)
    assert "What to do next" in html and "Offer gift wrap" in html and "/wrap" in html
    assert "Next: Offer gift wrap" in text


def test_tips_follow_the_shops_setup():
    from app.workers.digest import tips_for
    shop = make_shop(plan_tier="growth")
    shop.gift_settings = {"theme_check": {"embed": "off", "theme_name": "Dawn"}}
    titles = [t for t, _, _ in tips_for(shop)]
    assert titles == ["Turn the gift finder back on", "Offer gift wrap"]
    shop.gift_settings = {"wrap": {"enabled": True, "ready": True, "styles": [{"name": "G"}]},
                          "delivery": {"enabled": True}}
    assert tips_for(shop) == []


def test_links_never_have_an_empty_app_segment(monkeypatch):
    from app.services import email_style
    monkeypatch.setattr(email_style.core_settings, "shopify_app_handle", "")
    monkeypatch.setattr(email_style.core_settings, "shopify_api_key", "abc123")
    assert digest_email.app_url("snow.myshopify.com", "wrap") == "https://admin.shopify.com/store/snow/apps/abc123/wrap"
    _, html, text = digest_email.render("snow", "snow.myshopify.com", {"sessions": 0, "gift_orders": 0,
        "attributed_revenue": 0, "conversion_rate": None, "note_acceptance_rate": None, "all_orders": 0}, {},
        tips=[("Offer gift wrap", "x", "wrap")])
    assert "/apps//" not in html and "/apps//" not in text and "/apps/abc123/wrap" in html
    monkeypatch.setattr(email_style.core_settings, "shopify_app_handle", "giftsense-dev")
    assert digest_email.app_url("snow.myshopify.com") == "https://admin.shopify.com/store/snow/apps/giftsense-dev/analytics"


# ── Retention ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sessions_and_note_drafts_follow_the_promised_retention(db_session):
    from datetime import timedelta as td
    from app.workers.main import purge_old_gift_sessions
    from core.db.models import GiftSession
    shop = make_shop()
    db_session.add(shop)
    await db_session.flush()
    now = datetime.now(timezone.utc)
    rows = {
        "old": GiftSession(shop_id=shop.id, sid=uuid.uuid4(), intake={}, last_picks=[], updated_at=now - td(days=91)),
        "drafty": GiftSession(shop_id=shop.id, sid=uuid.uuid4(), intake={}, last_picks=[],
                              note_drafts={"order": {"count": 1, "last": "Hi"}}, updated_at=now - td(days=31)),
        "fresh": GiftSession(shop_id=shop.id, sid=uuid.uuid4(), intake={}, last_picks=[],
                             note_drafts={"order": {"count": 1, "last": "Hi"}}, updated_at=now - td(days=2)),
    }
    db_session.add_all(rows.values())
    await db_session.commit()
    with patch("app.workers.main.AsyncSessionLocal") as sess:
        sess.return_value.__aenter__.return_value = db_session
        sess.return_value.__aexit__.return_value = False
        result = await purge_old_gift_sessions({}, now)
    assert result == {"sessions_deleted": 1, "drafts_cleared": 1}
    from sqlalchemy import select as _select
    left = {str(r.sid): r for r in (await db_session.execute(_select(GiftSession))).scalars()}
    assert str(rows["old"].sid) not in left
    await db_session.refresh(rows["drafty"]); await db_session.refresh(rows["fresh"])
    assert rows["drafty"].note_drafts is None and rows["fresh"].note_drafts is not None
