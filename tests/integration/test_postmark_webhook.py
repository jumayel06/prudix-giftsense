"""Integration tests for POST /webhooks/postmark + send_email suppression check.

Covers the Postmark bounce/complaint ingestion path end-to-end:
  - Basic Auth guard (401 when creds don't match, 200 when unconfigured)
  - Hard bounce → row inserted in suppressed_emails
  - Soft bounce → ignored
  - Spam complaint → row inserted
  - Delivery/Open/Click record types → ignored
  - Duplicate suppression → 200 idempotent (no row conflict)
  - send_email() raises PostmarkSuppressed when recipient is suppressed
"""

import base64
import json

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.services import postmark_client
from core.db.models import SuppressedEmail
from core.db.session import AsyncSessionLocal, get_db


def _make_client(db_session):
    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app, raise_server_exceptions=True)
    yield client
    app.dependency_overrides.clear()


def _basic_auth(user: str, password: str) -> dict:
    creds = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


# ── Basic Auth guard ──────────────────────────────────────────────────────────

class TestPostmarkBasicAuth:

    @pytest.mark.asyncio
    async def test_missing_auth_401_when_configured(self, db_session, monkeypatch):
        from core import config as core_config
        monkeypatch.setattr(core_config.settings, "postmark_webhook_user", "postmark")
        monkeypatch.setattr(core_config.settings, "postmark_webhook_password", "s3cret")

        body = json.dumps({"RecordType": "Bounce", "Type": "HardBounce", "Email": "a@b.com"})
        for client in _make_client(db_session):
            resp = client.post("/webhooks/postmark", content=body)
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_password_401(self, db_session, monkeypatch):
        from core import config as core_config
        monkeypatch.setattr(core_config.settings, "postmark_webhook_user", "postmark")
        monkeypatch.setattr(core_config.settings, "postmark_webhook_password", "s3cret")

        body = json.dumps({"RecordType": "Bounce", "Type": "HardBounce", "Email": "a@b.com"})
        for client in _make_client(db_session):
            resp = client.post(
                "/webhooks/postmark",
                content=body,
                headers=_basic_auth("postmark", "wrong"),
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_correct_creds_pass(self, db_session, monkeypatch):
        from core import config as core_config
        monkeypatch.setattr(core_config.settings, "postmark_webhook_user", "postmark")
        monkeypatch.setattr(core_config.settings, "postmark_webhook_password", "s3cret")

        body = json.dumps({
            "RecordType": "Bounce", "Type": "HardBounce",
            "Email": "a@b.com", "MessageID": "msg-1",
        })
        for client in _make_client(db_session):
            resp = client.post(
                "/webhooks/postmark",
                content=body,
                headers=_basic_auth("postmark", "s3cret"),
            )
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_unconfigured_allows_all(self, db_session, monkeypatch):
        # Both env vars empty (dev) → no auth check, accept any request.
        from core import config as core_config
        monkeypatch.setattr(core_config.settings, "postmark_webhook_user", "")
        monkeypatch.setattr(core_config.settings, "postmark_webhook_password", "")

        body = json.dumps({
            "RecordType": "Bounce", "Type": "HardBounce",
            "Email": "a@b.com", "MessageID": "msg-1",
        })
        for client in _make_client(db_session):
            resp = client.post("/webhooks/postmark", content=body)
            assert resp.status_code == 200


# ── Event dispatch ────────────────────────────────────────────────────────────

class TestPostmarkEventDispatch:

    @pytest.mark.asyncio
    async def test_hard_bounce_inserts_row(self, db_session, monkeypatch):
        from core import config as core_config
        monkeypatch.setattr(core_config.settings, "postmark_webhook_user", "")
        monkeypatch.setattr(core_config.settings, "postmark_webhook_password", "")

        body = json.dumps({
            "RecordType": "Bounce",
            "Type": "HardBounce",
            "TypeCode": 1,
            "Email": "Bounced@Example.com",
            "MessageID": "abc-123",
        })
        for client in _make_client(db_session):
            resp = client.post("/webhooks/postmark", content=body)
            assert resp.status_code == 200
            assert resp.json()["suppressed"] is True
            assert resp.json()["reason"] == "hard_bounce"

        row = (await db_session.execute(
            select(SuppressedEmail).where(SuppressedEmail.email == "bounced@example.com")
        )).scalar_one()
        assert row.reason == "hard_bounce"
        assert row.postmark_message_id == "abc-123"
        assert row.source == "Bounce"

    @pytest.mark.asyncio
    async def test_soft_bounce_ignored(self, db_session, monkeypatch):
        from core import config as core_config
        monkeypatch.setattr(core_config.settings, "postmark_webhook_user", "")
        monkeypatch.setattr(core_config.settings, "postmark_webhook_password", "")

        body = json.dumps({
            "RecordType": "Bounce",
            "Type": "SoftBounce",
            "TypeCode": 2,
            "Email": "soft@example.com",
            "MessageID": "soft-1",
        })
        for client in _make_client(db_session):
            resp = client.post("/webhooks/postmark", content=body)
            assert resp.status_code == 200
            assert resp.json().get("ignored") == "Bounce"

        row = (await db_session.execute(
            select(SuppressedEmail).where(SuppressedEmail.email == "soft@example.com")
        )).scalar_one_or_none()
        assert row is None

    @pytest.mark.asyncio
    async def test_spam_complaint_inserts_row(self, db_session, monkeypatch):
        from core import config as core_config
        monkeypatch.setattr(core_config.settings, "postmark_webhook_user", "")
        monkeypatch.setattr(core_config.settings, "postmark_webhook_password", "")

        body = json.dumps({
            "RecordType": "SpamComplaint",
            "Email": "spammer@example.com",
            "MessageID": "sc-1",
        })
        for client in _make_client(db_session):
            resp = client.post("/webhooks/postmark", content=body)
            assert resp.status_code == 200
            assert resp.json()["reason"] == "spam_complaint"

        row = (await db_session.execute(
            select(SuppressedEmail).where(SuppressedEmail.email == "spammer@example.com")
        )).scalar_one()
        assert row.reason == "spam_complaint"
        assert row.source == "SpamComplaint"

    @pytest.mark.asyncio
    async def test_delivery_event_ignored(self, db_session, monkeypatch):
        from core import config as core_config
        monkeypatch.setattr(core_config.settings, "postmark_webhook_user", "")
        monkeypatch.setattr(core_config.settings, "postmark_webhook_password", "")

        body = json.dumps({
            "RecordType": "Delivery",
            "Email": "delivered@example.com",
            "MessageID": "d-1",
        })
        for client in _make_client(db_session):
            resp = client.post("/webhooks/postmark", content=body)
            assert resp.status_code == 200
            assert "ignored" in resp.json()

        row = (await db_session.execute(
            select(SuppressedEmail).where(SuppressedEmail.email == "delivered@example.com")
        )).scalar_one_or_none()
        assert row is None

    @pytest.mark.asyncio
    async def test_duplicate_bounce_idempotent(self, db_session, monkeypatch):
        from core import config as core_config
        monkeypatch.setattr(core_config.settings, "postmark_webhook_user", "")
        monkeypatch.setattr(core_config.settings, "postmark_webhook_password", "")

        body = json.dumps({
            "RecordType": "Bounce", "Type": "HardBounce", "TypeCode": 1,
            "Email": "dup@example.com", "MessageID": "dup-1",
        })
        for client in _make_client(db_session):
            r1 = client.post("/webhooks/postmark", content=body)
            r2 = client.post("/webhooks/postmark", content=body)
            assert r1.status_code == 200
            assert r2.status_code == 200
            assert r2.json().get("duplicate") is True

        rows = (await db_session.execute(
            select(SuppressedEmail).where(SuppressedEmail.email == "dup@example.com")
        )).scalars().all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_missing_email_ignored(self, db_session, monkeypatch):
        from core import config as core_config
        monkeypatch.setattr(core_config.settings, "postmark_webhook_user", "")
        monkeypatch.setattr(core_config.settings, "postmark_webhook_password", "")

        body = json.dumps({"RecordType": "Bounce", "Type": "HardBounce"})
        for client in _make_client(db_session):
            resp = client.post("/webhooks/postmark", content=body)
            assert resp.status_code == 200
            assert resp.json().get("ignored") == "no_email"


# ── send_email() suppression check ────────────────────────────────────────────

class TestSendEmailSuppression:

    @pytest.mark.asyncio
    async def test_suppressed_recipient_raises(self, engine, monkeypatch):
        # Point AsyncSessionLocal at the test engine so _is_suppressed()
        # inside send_email() reads from the same DB the fixture writes to.
        from sqlalchemy.ext.asyncio import async_sessionmaker
        test_factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(postmark_client, "AsyncSessionLocal", test_factory)

        async with test_factory() as db:
            import uuid as _uuid
            db.add(SuppressedEmail(
                id=_uuid.uuid4(),
                email="dead@example.com",
                reason="hard_bounce",
                postmark_message_id="msg-x",
                source="Bounce",
            ))
            await db.commit()

        with pytest.raises(postmark_client.PostmarkSuppressed) as excinfo:
            await postmark_client.send_email(
                to_email="Dead@Example.com",
                subject="Test",
                html_body="<p>hi</p>",
                text_body="hi",
            )
        assert excinfo.value.reason == "hard_bounce"
        assert excinfo.value.email == "Dead@Example.com"

    @pytest.mark.asyncio
    async def test_non_suppressed_recipient_proceeds(self, engine, monkeypatch):
        # No suppression row — send_email() should fall through to dev-mode
        # (server_token is "") and return the fake success response.
        from sqlalchemy.ext.asyncio import async_sessionmaker
        test_factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(postmark_client, "AsyncSessionLocal", test_factory)

        from core import config as core_config
        monkeypatch.setattr(core_config.settings, "postmark_server_token", "")

        resp = await postmark_client.send_email(
            to_email="fresh@example.com",
            subject="Test",
            html_body="<p>hi</p>",
            text_body="hi",
        )
        assert resp["dev_mode"] is True
        assert resp["to"] == "fresh@example.com"

    @pytest.mark.asyncio
    async def test_suppressed_subclass_caught_as_postmark_error(self, engine, monkeypatch):
        # Existing callers use `except PostmarkError` and set send_error.
        # Confirm PostmarkSuppressed still hits that branch.
        from sqlalchemy.ext.asyncio import async_sessionmaker
        test_factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(postmark_client, "AsyncSessionLocal", test_factory)

        async with test_factory() as db:
            import uuid as _uuid
            db.add(SuppressedEmail(
                id=_uuid.uuid4(),
                email="dead@example.com",
                reason="spam_complaint",
                postmark_message_id=None,
                source="SpamComplaint",
            ))
            await db.commit()

        with pytest.raises(postmark_client.PostmarkError):
            await postmark_client.send_email(
                to_email="dead@example.com",
                subject="Test",
                html_body="<p>hi</p>",
                text_body="hi",
            )
