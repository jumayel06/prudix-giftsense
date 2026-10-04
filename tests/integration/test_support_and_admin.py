"""Support tickets (dashboard → support@ email → /admin/support) and the
internal admin panel (/admin/*, Basic Auth)."""
import base64
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from core.config import settings
from core.db.models import Shop, SupportTicket, UsageLog
from core.db.session import get_db
from tests.conftest import TEST_SHOP_DOMAIN, make_shop

AUTH = {"Authorization": "Basic " + base64.b64encode(b"ops:s3cret").decode()}


@pytest.fixture
def admin_creds(monkeypatch):
    monkeypatch.setattr(settings, "internal_admin_username", "ops")
    monkeypatch.setattr(settings, "internal_admin_password", "s3cret")


def client(db):
    async def override_db():
        yield db
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, base_url="https://giftsense.test")


@pytest.fixture(autouse=True)
def _clear():
    yield
    app.dependency_overrides.clear()


# ── Merchant support ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ticket_is_stored_numbered_and_emailed_escaped(db_session):
    shop = make_shop()
    shop.shop_owner_email = "owner@snow.co"
    db_session.add(shop)
    await db_session.commit()
    send = AsyncMock(return_value={})
    with patch("app.routes.support.send_email", send):
        c = client(db_session)
        first = c.post(f"/api/support/ticket?shop={TEST_SHOP_DOMAIN}",
                       json={"category": "bug", "subject": "Wrap <b>broken</b>", "message": "<script>x</script>"})
        second = c.post(f"/api/support/ticket?shop={TEST_SHOP_DOMAIN}",
                        json={"category": "other", "subject": "Hi", "message": "Thanks"})
    assert first.status_code == 201 and first.json()["ticket_number"] == 1001
    assert second.json()["ticket_number"] == 1002
    kw = send.await_args_list[0].kwargs
    assert kw["to_email"] == "support@prudix.app" and kw["reply_to"] == "owner@snow.co"
    assert "<script>x</script>" not in kw["html_body"] and "&lt;script&gt;" in kw["html_body"]
    mine = c.get(f"/api/support/tickets?shop={TEST_SHOP_DOMAIN}").json()
    assert [t["ticket_number"] for t in mine] == [1002, 1001]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"category": "spam", "subject": "x", "message": "y"},
    {"category": "bug", "subject": "", "message": "y"},
    {"category": "bug", "subject": "x", "message": "y", "screenshot_url": "javascript:alert(1)"},
])
async def test_bad_tickets_are_refused(db_session, body):
    db_session.add(make_shop())
    await db_session.commit()
    with patch("app.routes.support.send_email", AsyncMock()):
        assert client(db_session).post(f"/api/support/ticket?shop={TEST_SHOP_DOMAIN}", json=body).status_code == 422


# ── Admin panel ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_needs_credentials(db_session, admin_creds):
    c = client(db_session)
    assert c.get("/admin/").status_code == 401
    assert c.get("/admin/", headers={"Authorization": "Basic " + base64.b64encode(b"ops:nope").decode()}).status_code == 401


@pytest.mark.asyncio
async def test_admin_off_without_a_password(db_session, monkeypatch):
    monkeypatch.setattr(settings, "internal_admin_password", "")
    assert client(db_session).get("/admin/", headers=AUTH).status_code == 503


@pytest.mark.asyncio
async def test_every_admin_page_renders(db_session, admin_creds):
    shop = make_shop(plan_tier="pro")
    db_session.add(shop)
    await db_session.flush()
    db_session.add(UsageLog(shop_id=shop.id, action_type="gift_search", generations_consumed=4,
                            model_used="claude-sonnet-5", cost_usd=0.02, created_at=datetime.now(timezone.utc)))
    db_session.add(SupportTicket(ticket_number=1001, shop_id=shop.id, category="bug", subject="<b>x</b>", message="m"))
    await db_session.commit()
    c = client(db_session)
    for path in ("/admin/", "/admin/financials", "/admin/shops", f"/admin/shops/{shop.id}", "/admin/models",
                 "/admin/support", "/admin/system"):
        resp = c.get(path, headers=AUTH)
        assert resp.status_code == 200, path
    page = c.get("/admin/support", headers=AUTH).text
    assert "&lt;b&gt;x&lt;/b&gt;" in page                                   # autoescaped
    assert "Claude Sonnet 5" in c.get("/admin/models", headers=AUTH).text
    assert "$99" in c.get("/admin/", headers=AUTH).text                       # pro MRR


@pytest.mark.asyncio
async def test_pins_and_ticket_updates(db_session, admin_creds):
    shop = make_shop(plan_tier="pro")
    db_session.add(shop)
    await db_session.flush()
    ticket = SupportTicket(ticket_number=1001, shop_id=shop.id, category="bug", subject="s", message="m")
    db_session.add(ticket)
    await db_session.commit()
    c = client(db_session)
    same = {**AUTH, "Origin": "https://giftsense.test"}

    resp = c.post(f"/admin/shops/{shop.id}/pins", headers=same, follow_redirects=False,
                  data={"ai_premium": "claude-sonnet-5-5", "ai_standard": ""})
    assert resp.status_code == 303
    await db_session.refresh(shop)
    assert shop.model_pins == {"ai_premium": "claude-sonnet-5-5"}
    assert c.post(f"/admin/shops/{shop.id}/pins", headers=same, data={"ai_premium": "gpt-4o-mini"}).status_code == 422
    assert c.post(f"/admin/shops/{shop.id}/pins", headers={**AUTH, "Origin": "https://evil.example"},
                  data={"ai_premium": ""}).status_code == 403                   # CSRF guard

    c.post(f"/admin/support/{ticket.id}/update", headers=same, data={"status": "resolved", "admin_notes": "done"})
    await db_session.refresh(ticket)
    assert ticket.status == "resolved" and ticket.admin_notes == "done" and ticket.resolved_at is not None


def test_worker_heartbeat_is_readable():
    from app.admin.router import _beat
    assert _beat("Oct-04 10:59:12 j_complete=214 j_failed=1 j_retried=0 j_ongoing=2 queued=0") == \
        "Last heartbeat 10:59:12 · 214 jobs done · 1 failed · 2 running"
