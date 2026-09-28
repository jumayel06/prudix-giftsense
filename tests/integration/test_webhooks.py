"""
Integration tests for the Shopify webhook handler.

Tests the full HTTP route stack using FastAPI TestClient + in-memory SQLite.
Each test exercises a distinct billing lifecycle state transition.

Key scenarios:
  - Invalid HMAC → 401 (security boundary)
  - Duplicate webhook ID → 200 idempotent (no double-processing)
  - app/uninstalled → shop marked uninstalled, token revoked
  - app_subscriptions/update (active, was trial) → billing_cycle_start reset
  - app_subscriptions/update (active, renewal) → billing_cycle_start updated
  - app_subscriptions/update (cancelled) → grace period set
  - app_subscriptions/update (declined) → plan_status = declined
"""

import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from core.db.session import get_db
from core.db.models import BillingEvent, ProcessedWebhook, Shop
from tests.conftest import make_shop, TEST_SHOP_DOMAIN, TEST_API_SECRET


# ── App + DB dependency override ──────────────────────────────────────────────

def _make_client(db_session):
    """Build a TestClient with the DB session overridden to use in-memory SQLite."""
    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app, raise_server_exceptions=True)
    yield client
    app.dependency_overrides.clear()


def _make_webhook_body(topic: str, payload: dict) -> bytes:
    return json.dumps(payload).encode()


def _sign(body: bytes, secret: str = TEST_API_SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _headers(body: bytes, topic: str, shop: str = TEST_SHOP_DOMAIN,
             webhook_id: str | None = None, secret: str = TEST_API_SECRET) -> dict:
    return {
        "X-Shopify-Topic": topic,
        "X-Shopify-Hmac-Sha256": _sign(body, secret),
        "X-Shopify-Webhook-Id": webhook_id or str(uuid.uuid4()),
        "X-Shopify-Shop-Domain": shop,
        "Content-Type": "application/json",
    }


# ── HMAC verification ─────────────────────────────────────────────────────────

class TestHmacVerification:

    @pytest.mark.asyncio
    async def test_invalid_hmac_returns_401(self, db_session):
        shop = make_shop()
        db_session.add(shop)
        await db_session.commit()

        body = _make_webhook_body("app/uninstalled", {"shop_domain": TEST_SHOP_DOMAIN})
        headers = _headers(body, "app/uninstalled", secret="wrong_secret")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_hmac_returns_200(self, db_session):
        shop = make_shop()
        db_session.add(shop)
        await db_session.commit()

        body = _make_webhook_body("app/uninstalled", {"shop_domain": TEST_SHOP_DOMAIN})
        headers = _headers(body, "app/uninstalled")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
            assert resp.status_code == 200


# ── Idempotency ───────────────────────────────────────────────────────────────

class TestIdempotency:

    @pytest.mark.asyncio
    async def test_duplicate_webhook_id_not_processed_twice(self, db_session):
        shop = make_shop()
        db_session.add(shop)
        await db_session.commit()

        webhook_id = str(uuid.uuid4())
        body = _make_webhook_body("app/uninstalled", {"shop_domain": TEST_SHOP_DOMAIN})
        headers = _headers(body, "app/uninstalled", webhook_id=webhook_id)

        for client in _make_client(db_session):
            # First request: processed
            resp1 = client.post("/webhooks", content=body, headers=headers)
            assert resp1.status_code == 200
            assert resp1.json().get("duplicate") is not True

            # Second request with same webhook_id: idempotent skip
            resp2 = client.post("/webhooks", content=body, headers=headers)
            assert resp2.status_code == 200
            assert resp2.json().get("duplicate") is True

        # ProcessedWebhook record should exist exactly once — full schema check
        result = await db_session.execute(
            select(ProcessedWebhook).where(ProcessedWebhook.webhook_id == webhook_id)
        )
        rows = result.scalars().all()
        assert len(rows) == 1
        pw = rows[0]
        assert pw.webhook_id == webhook_id
        assert pw.topic == "app/uninstalled"
        assert pw.received_at is not None

    @pytest.mark.asyncio
    async def test_different_webhook_ids_both_processed(self, db_session):
        shop = make_shop()
        db_session.add(shop)
        await db_session.commit()

        for client in _make_client(db_session):
            for _ in range(2):
                body = _make_webhook_body("app/uninstalled", {"shop_domain": TEST_SHOP_DOMAIN})
                headers = _headers(body, "app/uninstalled", webhook_id=str(uuid.uuid4()))
                resp = client.post("/webhooks", content=body, headers=headers)
                assert resp.status_code == 200
                assert resp.json().get("duplicate") is not True


# ── app/uninstalled ───────────────────────────────────────────────────────────

class TestAppUninstalled:

    @pytest.mark.asyncio
    async def test_uninstall_clears_token_and_sets_status(self, db_session):
        from datetime import timedelta
        from core.shopify_auth import encrypt_token
        shop = make_shop(plan_status="active", plan_tier="growth")
        shop.access_token_encrypted = encrypt_token("real_access_token")
        shop.refresh_token_encrypted = encrypt_token("real_refresh_token")
        shop.access_token_expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        shop.refresh_token_expires_at = datetime.now(timezone.utc) + timedelta(days=90)
        shop.shopify_charge_id = "charge_abc123"
        shop.billing_cycle_start = datetime.now(timezone.utc) - timedelta(days=5)
        # Backdate installed_at so the stale-webhook guard (300s window) doesn't fire
        shop.installed_at = datetime.now(timezone.utc) - timedelta(seconds=600)
        db_session.add(shop)
        await db_session.commit()

        body = _make_webhook_body("app/uninstalled", {"shop_domain": TEST_SHOP_DOMAIN})
        headers = _headers(body, "app/uninstalled")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
            assert resp.status_code == 200

        await db_session.refresh(shop)
        # Status fields
        assert shop.plan_status == "uninstalled"
        assert shop.plan_tier == "none"
        assert shop.uninstalled_at is not None
        assert shop.data_purge_at is not None
        # All credentials and billing state must be cleared
        assert shop.access_token_encrypted == ""
        assert shop.refresh_token_encrypted is None
        assert shop.access_token_expires_at is None
        assert shop.refresh_token_expires_at is None
        assert shop.shopify_charge_id is None
        assert shop.billing_cycle_start is None

    @pytest.mark.asyncio
    async def test_uninstall_unknown_shop_is_noop(self, db_session):
        body = _make_webhook_body("app/uninstalled", {"shop_domain": "unknown.myshopify.com"})
        headers = _headers(body, "app/uninstalled", shop="unknown.myshopify.com")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
            assert resp.status_code == 200


# ── app_subscriptions/update ──────────────────────────────────────────────────

class TestSubscriptionUpdate:

    @pytest.mark.asyncio
    async def test_trial_converts_to_active_resets_billing_cycle(self, db_session):
        """Trial conversion must reset billing_cycle_start so trial usage
        doesn't count against the first paid month."""
        old_cycle = datetime.now(timezone.utc) - timedelta(days=7)
        shop = make_shop(plan_status="trial_active", billing_cycle_start=old_cycle)
        # Seed real trial timestamps — trial has already ended (7-day trial started 7 days ago)
        shop.trial_started_at = datetime.now(timezone.utc) - timedelta(days=7)
        # trial_ends_at in the past: webhook fires because billing is now starting
        shop.trial_ends_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db_session.add(shop)
        await db_session.commit()

        payload = {
            "app_subscription": {
                "admin_graphql_api_id": "gid://shopify/AppSubscription/123",
                "status": "active",
            }
        }
        body = _make_webhook_body("app_subscriptions/update", payload)
        headers = _headers(body, "app_subscriptions/update")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
            assert resp.status_code == 200

        await db_session.refresh(shop)
        assert shop.plan_status == "active"
        assert shop.plan_tier == "growth"
        # billing_cycle_start reset so trial usage doesn't count against first paid month
        new_cycle = shop.billing_cycle_start
        if new_cycle.tzinfo is not None:
            new_cycle = new_cycle.replace(tzinfo=None)
        assert new_cycle > old_cycle.replace(tzinfo=None)
        # Grace period must NOT be set — this is a conversion, not a cancellation
        assert shop.grace_period_ends_at is None
        # charge ID captured from webhook payload
        assert shop.shopify_charge_id is not None
        # trial timestamps preserved as historical records (webhook does not clear them)
        assert shop.trial_started_at is not None
        assert shop.trial_ends_at is not None
        # Not an uninstall — no purge date
        assert shop.data_purge_at is None
        assert shop.uninstalled_at is None

        # BillingEvent must record trial_converted with correct tier and charge ID
        result = await db_session.execute(
            select(BillingEvent).where(BillingEvent.shop_id == shop.id)
        )
        event = result.scalar_one_or_none()
        assert event is not None
        assert event.event_type == "trial_converted"
        assert event.plan_tier == shop.plan_tier
        assert event.shopify_charge_id is not None
        assert event.created_at is not None

    @pytest.mark.asyncio
    async def test_renewal_resets_billing_cycle(self, db_session):
        old_cycle = datetime.now(timezone.utc) - timedelta(days=30)
        shop = make_shop(plan_status="active", billing_cycle_start=old_cycle)
        db_session.add(shop)
        await db_session.commit()

        payload = {
            "app_subscription": {
                "admin_graphql_api_id": "gid://shopify/AppSubscription/456",
                "status": "active",
            }
        }
        body = _make_webhook_body("app_subscriptions/update", payload)
        headers = _headers(body, "app_subscriptions/update")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
            assert resp.status_code == 200

        await db_session.refresh(shop)
        assert shop.plan_status == "active"
        new_cycle = shop.billing_cycle_start
        if new_cycle.tzinfo is not None:
            new_cycle = new_cycle.replace(tzinfo=None)
        assert new_cycle > old_cycle.replace(tzinfo=None)
        # Grace period must not be set on renewal
        assert shop.grace_period_ends_at is None
        # Plan tier unchanged
        assert shop.plan_tier == "growth"
        # charge ID updated to the new subscription's numeric ID
        assert shop.shopify_charge_id == "456"
        # Not an uninstall — no purge date
        assert shop.data_purge_at is None
        assert shop.uninstalled_at is None

        result = await db_session.execute(
            select(BillingEvent).where(BillingEvent.shop_id == shop.id)
        )
        event = result.scalar_one_or_none()
        assert event.event_type == "renewed"
        assert event.plan_tier == "growth"
        assert event.shopify_charge_id is not None
        assert event.created_at is not None

    @pytest.mark.asyncio
    async def test_cancelled_sets_grace_period(self, db_session):
        shop = make_shop(plan_status="active")
        # Charge ID must match the webhook payload to be treated as a genuine cancellation
        shop.shopify_charge_id = "789"
        db_session.add(shop)
        await db_session.commit()

        payload = {
            "app_subscription": {
                "admin_graphql_api_id": "gid://shopify/AppSubscription/789",
                "status": "cancelled",
            }
        }
        body = _make_webhook_body("app_subscriptions/update", payload)
        headers = _headers(body, "app_subscriptions/update")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
            assert resp.status_code == 200

        await db_session.refresh(shop)
        assert shop.plan_status == "cancelled"
        assert shop.plan_tier == "growth"     # unchanged on cancel
        # Grace period must be set and in the future
        assert shop.grace_period_ends_at is not None
        grace = shop.grace_period_ends_at.replace(tzinfo=None) if shop.grace_period_ends_at.tzinfo else shop.grace_period_ends_at
        assert grace > datetime.now().replace(tzinfo=None)
        # billing_cycle_start preserved — needed to compute usage during grace period
        assert shop.billing_cycle_start is not None
        # charge ID preserved — still the same subscription (stored as numeric)
        assert shop.shopify_charge_id == "789"
        # Trial fields unchanged (not set in fixture → remain None)
        assert shop.trial_started_at is None
        assert shop.trial_ends_at is None
        # Cancellation is NOT an uninstall — no purge date, not uninstalled
        assert shop.data_purge_at is None
        assert shop.uninstalled_at is None

        # BillingEvent recorded
        result = await db_session.execute(
            select(BillingEvent).where(BillingEvent.shop_id == shop.id)
        )
        event = result.scalar_one_or_none()
        assert event is not None
        assert event.event_type == "cancelled"
        assert event.plan_tier == "growth"
        assert event.created_at is not None

    @pytest.mark.asyncio
    async def test_declined_sets_declined_status(self, db_session):
        old_cycle = datetime.now(timezone.utc) - timedelta(days=10)
        shop = make_shop(plan_status="active", billing_cycle_start=old_cycle)
        # Charge ID must match the webhook payload to be treated as a genuine decline
        shop.shopify_charge_id = "999"
        db_session.add(shop)
        await db_session.commit()

        payload = {
            "app_subscription": {
                "admin_graphql_api_id": "gid://shopify/AppSubscription/999",
                "status": "declined",
            }
        }
        body = _make_webhook_body("app_subscriptions/update", payload)
        headers = _headers(body, "app_subscriptions/update")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
            assert resp.status_code == 200

        await db_session.refresh(shop)
        assert shop.plan_status == "declined"
        # Declined does NOT get a grace period (unlike cancelled/expired)
        assert shop.grace_period_ends_at is None
        # Plan tier unchanged
        assert shop.plan_tier == "growth"
        # billing_cycle_start preserved (not cleared on declined)
        assert shop.billing_cycle_start is not None
        old_cycle_naive = old_cycle.replace(tzinfo=None)
        current_cycle = shop.billing_cycle_start.replace(tzinfo=None) if shop.billing_cycle_start.tzinfo else shop.billing_cycle_start
        assert abs((current_cycle - old_cycle_naive).total_seconds()) < 5
        # charge ID preserved — still the same subscription that was declined (stored as numeric)
        assert shop.shopify_charge_id == "999"
        # Trial fields not set in fixture → remain None
        assert shop.trial_started_at is None
        assert shop.trial_ends_at is None
        # Decline is NOT an uninstall
        assert shop.data_purge_at is None
        assert shop.uninstalled_at is None

        # BillingEvent recorded with declined event_type
        result = await db_session.execute(
            select(BillingEvent).where(BillingEvent.shop_id == shop.id)
        )
        event = result.scalar_one_or_none()
        assert event is not None
        assert event.event_type == "declined"
        assert event.plan_tier == "growth"
        assert event.created_at is not None

    @pytest.mark.asyncio
    async def test_first_activation_sets_billing_cycle_start(self, db_session):
        """Fresh install with no prior billing_cycle_start — first activation must set it."""
        shop = make_shop(plan_status="pending", billing_cycle_start=None, trial_used=True)
        shop.billing_cycle_start = None
        db_session.add(shop)
        await db_session.commit()

        payload = {
            "app_subscription": {
                "admin_graphql_api_id": "gid://shopify/AppSubscription/1",
                "status": "active",
            }
        }
        body = _make_webhook_body("app_subscriptions/update", payload)
        headers = _headers(body, "app_subscriptions/update")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
            assert resp.status_code == 200

        await db_session.refresh(shop)
        assert shop.plan_status == "active"
        assert shop.plan_tier == "growth"   # fixture default; no name in payload so tier preserved
        assert shop.billing_cycle_start is not None
        assert shop.shopify_charge_id is not None
        assert shop.grace_period_ends_at is None
        # Not an uninstall
        assert shop.data_purge_at is None
        assert shop.uninstalled_at is None

        # BillingEvent created for first activation
        result = await db_session.execute(
            select(BillingEvent).where(BillingEvent.shop_id == shop.id)
        )
        event = result.scalar_one_or_none()
        assert event is not None
        assert event.event_type in ("activated", "renewed", "trial_converted")
        assert event.shop_id == shop.id
        assert event.plan_tier == "growth"
        assert event.created_at is not None


# ── GDPR endpoints ────────────────────────────────────────────────────────────

class TestGdprEndpoints:

    def test_data_request_returns_200(self, db_session):
        body = json.dumps({"shop_domain": TEST_SHOP_DOMAIN}).encode()
        for client in _make_client(db_session):
            resp = client.post("/webhooks/gdpr/customers/data_request",
                               content=body,
                               headers={"X-Shopify-Hmac-Sha256": _sign(body)})
            assert resp.status_code == 200

    def test_customers_redact_returns_200(self, db_session):
        body = json.dumps({"shop_domain": TEST_SHOP_DOMAIN}).encode()
        for client in _make_client(db_session):
            resp = client.post("/webhooks/gdpr/customers/redact",
                               content=body,
                               headers={"X-Shopify-Hmac-Sha256": _sign(body)})
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_shop_redact_purges_all_content_and_anonymises_shop(self, db_session):
        """shop/redact must delete all content tables for the shop and anonymise
        every sensitive field on the shop row."""
        import uuid as _uuid
        from core.shopify_auth import encrypt_token
        from core.db.models import BillingEvent, Job, UsageLog

        shop = make_shop(plan_status="uninstalled", plan_tier="growth")
        shop.access_token_encrypted = encrypt_token("sensitive_token")
        shop.refresh_token_encrypted = encrypt_token("sensitive_refresh")
        shop.shop_owner_email = "owner@example.com"
        shop.scheduled_plan_tier = "starter"
        shop.scheduled_change_at = datetime.now(timezone.utc) + timedelta(days=3)
        shop.shopify_charge_id = "charge_123"
        shop.billing_cycle_start = datetime.now(timezone.utc) - timedelta(days=5)
        shop.trial_started_at = datetime.now(timezone.utc) - timedelta(days=10)
        shop.trial_ends_at = datetime.now(timezone.utc) - timedelta(days=3)
        shop.grace_period_ends_at = datetime.now(timezone.utc) - timedelta(days=1)
        shop.data_purge_at = datetime.now(timezone.utc) + timedelta(days=1)
        db_session.add(shop)
        await db_session.commit()

        # Populate every content table so we can verify deletion
        job = Job(id=_uuid.uuid4(), shop_id=shop.id, action_type="catalog_sync", status="done")
        db_session.add(job)
        await db_session.flush()

        db_session.add(UsageLog(id=_uuid.uuid4(), shop_id=shop.id, action_type="gift_search", model_used="claude-haiku-4-5", job_id=job.id))
        db_session.add(BillingEvent(id=_uuid.uuid4(), shop_id=shop.id, event_type="trial_started", plan_tier="growth"))

        await db_session.commit()

        body = json.dumps({"shop_domain": TEST_SHOP_DOMAIN}).encode()
        for client in _make_client(db_session):
            resp = client.post("/webhooks/gdpr/shop/redact",
                               content=body,
                               headers={"X-Shopify-Hmac-Sha256": _sign(body)})
            assert resp.status_code == 200

        await db_session.refresh(shop)

        # Shop row anonymised — sensitive fields cleared
        assert shop.access_token_encrypted == ""
        assert shop.refresh_token_encrypted is None
        assert shop.shop_owner_email is None
        assert shop.scheduled_plan_tier is None
        assert shop.scheduled_change_at is None
        assert shop.shopify_charge_id is None
        assert shop.billing_cycle_start is None
        assert shop.trial_started_at is None
        assert shop.trial_ends_at is None
        assert shop.grace_period_ends_at is None
        assert shop.data_purge_at is None

        # Status reflects purged state
        assert shop.plan_status == "purged"
        assert shop.plan_tier == "none"

        # Defaults reset to cheapest model
        assert shop.selected_model == "claude-haiku-4-5"
        assert shop.review_prompt_shown is False

        # trial_used must survive purge — prevents re-trial after reinstall
        assert shop.trial_used is not None  # value preserved (whatever it was)

        # All content tables emptied for this shop
        from sqlalchemy import select as _select, func
        for Model in (UsageLog, BillingEvent, Job):
            count_col = list(Model.__table__.primary_key.columns)[0]
            result = await db_session.execute(
                _select(func.count()).select_from(Model).where(
                    Model.shop_id == shop.id
                )
            )
            assert result.scalar() == 0, f"{Model.__tablename__} still has rows after shop/redact"


# ── Webhook body / parsing edge cases ─────────────────────────────────────────

class TestWebhookBodyEdgeCases:

    @pytest.mark.asyncio
    async def test_invalid_json_body_still_returns_200(self, db_session):
        """Malformed JSON in webhook body triggers the except handler, returns 200."""
        shop = make_shop()
        db_session.add(shop)
        await db_session.commit()

        body = b"not valid json {"
        headers = _headers(body, "app/uninstalled")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
        assert resp.status_code == 200

    def test_shop_redact_invalid_json_still_returns_200(self, db_session):
        """Invalid JSON to GDPR shop/redact triggers exception handler, returns 200."""
        body = b"invalid json {"
        for client in _make_client(db_session):
            resp = client.post(
                "/webhooks/gdpr/shop/redact",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Shopify-Hmac-Sha256": _sign(body),
                },
            )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ── subscription update edge cases ────────────────────────────────────────────

class TestSubscriptionUpdateEdgeCases:

    @pytest.mark.asyncio
    async def test_unknown_shop_is_noop(self, db_session):
        """subscription update for a shop_domain not in DB is a no-op (returns 200)."""
        payload = {
            "app_subscription": {
                "admin_graphql_api_id": "gid://shopify/AppSubscription/999",
                "status": "active",
            }
        }
        body = _make_webhook_body("app_subscriptions/update", payload)
        headers = _headers(body, "app_subscriptions/update", shop="unknown.myshopify.com")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_expired_status_sets_grace_period(self, db_session):
        """expired status → shop.plan_status = 'expired' and grace_period_ends_at set."""
        shop = make_shop(plan_status="active")
        db_session.add(shop)
        await db_session.commit()

        payload = {
            "app_subscription": {
                "admin_graphql_api_id": "gid://shopify/AppSubscription/999",
                "status": "expired",
            }
        }
        body = _make_webhook_body("app_subscriptions/update", payload)
        headers = _headers(body, "app_subscriptions/update")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
        assert resp.status_code == 200

        await db_session.refresh(shop)
        assert shop.plan_status == "expired"
        assert shop.grace_period_ends_at is not None
        # Expired plan: tier and billing state unchanged
        assert shop.plan_tier == "growth"
        assert shop.billing_cycle_start is not None
        # Expiry is NOT an uninstall
        assert shop.data_purge_at is None
        assert shop.uninstalled_at is None

    @pytest.mark.asyncio
    async def test_unknown_status_records_event_with_status_as_type(self, db_session):
        """Unrecognised status falls through to else branch, event_type = status string."""
        shop = make_shop(plan_status="active")
        db_session.add(shop)
        await db_session.commit()

        payload = {
            "app_subscription": {
                "admin_graphql_api_id": "gid://shopify/AppSubscription/999",
                "status": "frozen",
            }
        }
        body = _make_webhook_body("app_subscriptions/update", payload)
        headers = _headers(body, "app_subscriptions/update")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
        assert resp.status_code == 200

        await db_session.refresh(shop)
        # Unknown status falls through to else — plan_status is NOT changed
        assert shop.plan_status == "active"
        assert shop.grace_period_ends_at is None

        result = await db_session.execute(
            select(BillingEvent).where(BillingEvent.shop_id == shop.id)
        )
        event = result.scalar_one_or_none()
        assert event is not None
        assert event.event_type == "frozen"
        assert event.created_at is not None


# ── Uninstall preserves non-credential fields ─────────────────────────────────

class TestUninstallPreservesFields:

    @pytest.mark.asyncio
    async def test_uninstall_preserves_review_prompt_shown(self, db_session):
        """review_prompt_shown must be preserved through uninstall — it tracks a one-time UX event."""
        from core.shopify_auth import encrypt_token
        shop = make_shop(plan_status="active", review_prompt_shown=True)
        shop.access_token_encrypted = encrypt_token("tok")
        shop.installed_at = datetime.now(timezone.utc) - timedelta(seconds=600)
        db_session.add(shop)
        await db_session.commit()

        body = _make_webhook_body("app/uninstalled", {"shop_domain": TEST_SHOP_DOMAIN})
        headers = _headers(body, "app/uninstalled")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
            assert resp.status_code == 200

        await db_session.refresh(shop)
        assert shop.review_prompt_shown is True

    @pytest.mark.asyncio
    async def test_uninstall_preserves_store_timezone(self, db_session):
        """store_timezone must survive uninstall — it is merchant configuration, not a credential."""
        from core.shopify_auth import encrypt_token
        shop = make_shop(plan_status="active", store_timezone="America/New_York")
        shop.access_token_encrypted = encrypt_token("tok")
        shop.installed_at = datetime.now(timezone.utc) - timedelta(seconds=600)
        db_session.add(shop)
        await db_session.commit()

        body = _make_webhook_body("app/uninstalled", {"shop_domain": TEST_SHOP_DOMAIN})
        headers = _headers(body, "app/uninstalled")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
            assert resp.status_code == 200

        await db_session.refresh(shop)
        assert shop.store_timezone == "America/New_York"

    @pytest.mark.asyncio
    async def test_uninstall_preserves_trial_used_true(self, db_session):
        """trial_used=True must survive uninstall — prevents re-trial on reinstall."""
        from core.shopify_auth import encrypt_token
        shop = make_shop(plan_status="active", trial_used=True)
        shop.access_token_encrypted = encrypt_token("tok")
        shop.installed_at = datetime.now(timezone.utc) - timedelta(seconds=600)
        db_session.add(shop)
        await db_session.commit()

        body = _make_webhook_body("app/uninstalled", {"shop_domain": TEST_SHOP_DOMAIN})
        headers = _headers(body, "app/uninstalled")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
            assert resp.status_code == 200

        await db_session.refresh(shop)
        assert shop.trial_used is True

    @pytest.mark.asyncio
    async def test_authoritative_flag_bypasses_stale_check(self, db_session):
        """When `authoritative=True`, the stale check is skipped — for /auth and
        the reconciliation cron, which both confirm via a live 401 before calling.
        Even a fresh install (installed_at within 60s) gets processed."""
        from app.routes.webhooks import _handle_uninstalled
        from core.shopify_auth import encrypt_token

        shop = make_shop(plan_status="active")
        shop.access_token_encrypted = encrypt_token("tok")
        shop.installed_at = datetime.now(timezone.utc)  # brand new — would normally be stale
        db_session.add(shop)
        await db_session.commit()

        await _handle_uninstalled(TEST_SHOP_DOMAIN, db_session, authoritative=True)
        await db_session.refresh(shop)
        assert shop.plan_status == "uninstalled"

    @pytest.mark.asyncio
    async def test_non_authoritative_call_still_applies_stale_check(self, db_session):
        """Webhook path (authoritative=False, no triggered_at) still hits the
        60s guard for very fresh installs — protects against Shopify replaying
        a stale webhook from the previous session."""
        from app.routes.webhooks import _handle_uninstalled
        from core.shopify_auth import encrypt_token

        shop = make_shop(plan_status="active")
        shop.access_token_encrypted = encrypt_token("tok")
        shop.installed_at = datetime.now(timezone.utc)  # brand new
        db_session.add(shop)
        await db_session.commit()

        await _handle_uninstalled(TEST_SHOP_DOMAIN, db_session)
        await db_session.refresh(shop)
        assert shop.plan_status == "active"  # stale check blocked the update


# ── shop/redact preserves fields and skips active shops ───────────────────────

class TestShopRedactEdgeCases:

    @pytest.mark.asyncio
    async def test_shop_redact_preserves_trial_used(self, db_session):
        """trial_used must survive shop/redact — prevents re-trial on reinstall even after purge."""
        from core.shopify_auth import encrypt_token
        shop = make_shop(plan_status="uninstalled", trial_used=True)
        shop.access_token_encrypted = encrypt_token("tok")
        db_session.add(shop)
        await db_session.commit()

        body = json.dumps({"shop_domain": TEST_SHOP_DOMAIN}).encode()
        for client in _make_client(db_session):
            resp = client.post("/webhooks/gdpr/shop/redact",
                               content=body,
                               headers={"X-Shopify-Hmac-Sha256": _sign(body)})
            assert resp.status_code == 200

        await db_session.refresh(shop)
        assert shop.trial_used is True

    @pytest.mark.asyncio
    async def test_shop_redact_preserves_store_timezone(self, db_session):
        """store_timezone must survive shop/redact (it is non-sensitive config, not PII)."""
        from core.shopify_auth import encrypt_token
        shop = make_shop(plan_status="uninstalled", store_timezone="Europe/Berlin")
        shop.access_token_encrypted = encrypt_token("tok")
        db_session.add(shop)
        await db_session.commit()

        body = json.dumps({"shop_domain": TEST_SHOP_DOMAIN}).encode()
        for client in _make_client(db_session):
            resp = client.post("/webhooks/gdpr/shop/redact",
                               content=body,
                               headers={"X-Shopify-Hmac-Sha256": _sign(body)})
            assert resp.status_code == 200

        await db_session.refresh(shop)
        assert shop.store_timezone == "Europe/Berlin"

    def test_shop_redact_nonexistent_shop_returns_200(self, db_session):
        """shop/redact for an unknown shop domain must return 200 (idempotent)."""
        body = json.dumps({"shop_domain": "nobody.myshopify.com"}).encode()
        for client in _make_client(db_session):
            resp = client.post("/webhooks/gdpr/shop/redact",
                               content=body,
                               headers={"X-Shopify-Hmac-Sha256": _sign(body)})
        assert resp.status_code == 200


# ── Subscription webhook idempotency ──────────────────────────────────────────

class TestSubscriptionWebhookIdempotency:

    @pytest.mark.asyncio
    async def test_subscription_update_duplicate_is_idempotent(self, db_session):
        """A second subscription/update with the same webhook_id must be skipped."""
        shop = make_shop(plan_status="active")
        db_session.add(shop)
        await db_session.commit()

        webhook_id = str(uuid.uuid4())
        payload = {
            "app_subscription": {
                "admin_graphql_api_id": "gid://shopify/AppSubscription/777",
                "status": "cancelled",
            }
        }
        body = _make_webhook_body("app_subscriptions/update", payload)
        headers = _headers(body, "app_subscriptions/update", webhook_id=webhook_id)

        for client in _make_client(db_session):
            resp1 = client.post("/webhooks", content=body, headers=headers)
            assert resp1.status_code == 200

            resp2 = client.post("/webhooks", content=body, headers=headers)
            assert resp2.status_code == 200
            assert resp2.json().get("duplicate") is True

        # Only one ProcessedWebhook row must exist
        result = await db_session.execute(
            select(ProcessedWebhook).where(ProcessedWebhook.webhook_id == webhook_id)
        )
        assert len(result.scalars().all()) == 1

        # Only one BillingEvent must have been created (not two)
        from sqlalchemy import func as _func
        count_result = await db_session.execute(
            select(_func.count()).select_from(BillingEvent).where(
                BillingEvent.shop_id == shop.id
            )
        )
        assert count_result.scalar() == 1


# ── _extract_numeric_id unit tests ────────────────────────────────────────────

class TestExtractNumericId:
    """Unit tests for the _extract_numeric_id helper used in subscription webhooks."""

    def test_gid_returns_numeric_part(self):
        from app.routes.webhooks import _extract_numeric_id
        assert _extract_numeric_id("gid://shopify/AppSubscription/123") == "123"

    def test_plain_numeric_passthrough(self):
        from app.routes.webhooks import _extract_numeric_id
        assert _extract_numeric_id("456") == "456"

    def test_nested_path_returns_last_segment(self):
        from app.routes.webhooks import _extract_numeric_id
        assert _extract_numeric_id("gid://shopify/AppSubscription/789") == "789"

    def test_empty_string_returns_empty(self):
        from app.routes.webhooks import _extract_numeric_id
        assert _extract_numeric_id("") == ""

    def test_no_slash_returns_value_unchanged(self):
        from app.routes.webhooks import _extract_numeric_id
        assert _extract_numeric_id("99999") == "99999"


# ── Stale-detection: GID normalization ────────────────────────────────────────

class TestSubscriptionUpdateStaleDetection:
    """
    When a merchant upgrades, Shopify cancels the old subscription after
    activating the new one. The CANCELLED webhook for the old subscription must
    be ignored. _extract_numeric_id ensures comparisons work even when the
    webhook uses the full GID format and the DB stores the numeric part.
    """

    @pytest.mark.asyncio
    async def test_cancelled_with_matching_charge_id_applies(self, db_session):
        """A CANCELLED webhook whose charge ID matches the current one IS applied."""
        shop = make_shop(plan_status="active", shopify_charge_id="100")
        db_session.add(shop)
        await db_session.commit()

        payload = {
            "app_subscription": {
                "admin_graphql_api_id": "gid://shopify/AppSubscription/100",
                "name": "Growth plan",
                "status": "cancelled",
            }
        }
        body = _make_webhook_body("app_subscriptions/update", payload)
        headers = _headers(body, "app_subscriptions/update")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
        assert resp.status_code == 200

        result = await db_session.execute(
            select(Shop).where(Shop.shop_domain == TEST_SHOP_DOMAIN)
        )
        shop = result.scalar_one()
        assert shop.plan_status == "cancelled"

    @pytest.mark.asyncio
    async def test_cancelled_with_different_charge_id_is_ignored(self, db_session):
        """CANCELLED for an old charge ID (upgrade scenario) must not change status."""
        shop = make_shop(plan_status="active", shopify_charge_id="200")  # new plan charge
        db_session.add(shop)
        await db_session.commit()

        payload = {
            "app_subscription": {
                "admin_graphql_api_id": "gid://shopify/AppSubscription/100",  # old charge
                "name": "Starter plan",
                "status": "cancelled",
            }
        }
        body = _make_webhook_body("app_subscriptions/update", payload)
        headers = _headers(body, "app_subscriptions/update")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
        assert resp.status_code == 200

        result = await db_session.execute(
            select(Shop).where(Shop.shop_domain == TEST_SHOP_DOMAIN)
        )
        shop = result.scalar_one()
        assert shop.plan_status == "active"  # unchanged — stale cancellation ignored

    @pytest.mark.asyncio
    async def test_cancelled_gid_matches_numeric_stored_charge(self, db_session):
        """Webhook sends GID; DB stores numeric. Normalization ensures they match."""
        shop = make_shop(plan_status="active", shopify_charge_id="300")  # numeric in DB
        db_session.add(shop)
        await db_session.commit()

        payload = {
            "app_subscription": {
                "admin_graphql_api_id": "gid://shopify/AppSubscription/300",  # GID in webhook
                "name": "Growth plan",
                "status": "cancelled",
            }
        }
        body = _make_webhook_body("app_subscriptions/update", payload)
        headers = _headers(body, "app_subscriptions/update")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
        assert resp.status_code == 200

        result = await db_session.execute(
            select(Shop).where(Shop.shop_domain == TEST_SHOP_DOMAIN)
        )
        shop = result.scalar_one()
        # Charge IDs match after normalization → cancellation applied
        assert shop.plan_status == "cancelled"

    @pytest.mark.asyncio
    async def test_cancelled_stale_tier_is_ignored(self, db_session):
        """CANCELLED for a lower-tier name while shop is on a higher tier = stale, ignored."""
        shop = make_shop(plan_tier="growth", plan_status="active", shopify_charge_id=None)
        db_session.add(shop)
        await db_session.commit()

        payload = {
            "app_subscription": {
                "admin_graphql_api_id": "gid://shopify/AppSubscription/50",
                "name": "Starter plan",   # lower tier than current "growth"
                "status": "cancelled",
            }
        }
        body = _make_webhook_body("app_subscriptions/update", payload)
        headers = _headers(body, "app_subscriptions/update")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
        assert resp.status_code == 200

        result = await db_session.execute(
            select(Shop).where(Shop.shop_domain == TEST_SHOP_DOMAIN)
        )
        shop = result.scalar_one()
        assert shop.plan_status == "active"  # stale tier detection ignored the cancellation


class TestWebhookIdFallbackDeterministic:
    """Ported from Commerce's 2026-09-27 fix: header-less webhooks dedupe on
    sha256(topic|shop|body), not a random uuid4 (which never deduped)."""

    @pytest.mark.asyncio
    async def test_redelivered_headerless_webhook_is_duplicate(self, db_session):
        shop = make_shop()
        db_session.add(shop)
        await db_session.commit()

        body = json.dumps({"app_subscription": {"admin_graphql_api_id": "gid://shopify/AppSubscription/1",
                                                "status": "frozen"}}).encode()
        headers = _headers(body, "app_subscriptions/update")
        headers.pop("X-Shopify-Webhook-Id", None)
        headers.pop("x-shopify-webhook-id", None)

        for client in _make_client(db_session):
            first = client.post("/webhooks", content=body, headers=headers).json()
            second = client.post("/webhooks", content=body, headers=headers).json()
        assert first.get("duplicate") is not True
        assert second.get("duplicate") is True
