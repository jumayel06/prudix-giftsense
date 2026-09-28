"""
Shared fixtures for the test suite.

Unit tests: use `mock_db` (AsyncMock-based, no real DB).
Integration tests: use `db_session` (real in-memory SQLite via aiosqlite).

External services (OpenAI, Anthropic, Shopify API, httpx) are always mocked —
no real API calls in tests.
"""

import hashlib
import hmac
import base64
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.db.models import Base, Shop, UsageLog


# ── Constants ─────────────────────────────────────────────────────────────────

TEST_SHOP_DOMAIN = "testshop.myshopify.com"
TEST_API_SECRET = "test_api_secret_12345"
TEST_ENCRYPTION_KEY = Fernet.generate_key().decode()


# ── In-memory SQLite engine (integration tests) ───────────────────────────────

@pytest_asyncio.fixture(scope="function")
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session(engine):
    """Real async DB session backed by in-memory SQLite."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session


# ── Mock DB session (unit tests) ──────────────────────────────────────────────

@pytest.fixture
def mock_db():
    """AsyncMock DB session — use when you want to control query results exactly."""
    session = AsyncMock(spec=AsyncSession)
    return session


# ── Shop factory ──────────────────────────────────────────────────────────────

def make_shop(
    *,
    domain: str = TEST_SHOP_DOMAIN,
    plan_tier: str = "growth",
    plan_status: str = "active",
    billing_cycle_start: datetime | None = None,
    trial_ends_at: datetime | None = None,
    trial_used: bool = False,
    selected_model: str = "claude-haiku-4-5",
    review_prompt_shown: bool = False,
    store_timezone: str = "UTC",
    refresh_token_encrypted: str | None = None,
    shopify_charge_id: str | None = None,
    trial_started_at: datetime | None = None,
    installed_at: datetime | None = None,
    uninstalled_at: datetime | None = None,
    data_purge_at: datetime | None = None,
    grace_period_ends_at: datetime | None = None,
) -> Shop:
    """Create a Shop ORM object with sensible defaults. Does NOT hit the DB."""
    return Shop(
        id=uuid.uuid4(),
        shop_domain=domain,
        access_token_encrypted="encrypted_token",
        plan_tier=plan_tier,
        plan_status=plan_status,
        billing_cycle_start=billing_cycle_start or datetime.now(timezone.utc) - timedelta(days=5),
        trial_ends_at=trial_ends_at,
        trial_used=trial_used,
        selected_model=selected_model,
        review_prompt_shown=review_prompt_shown,
        store_timezone=store_timezone,
        refresh_token_encrypted=refresh_token_encrypted,
        shopify_charge_id=shopify_charge_id,
        trial_started_at=trial_started_at,
        installed_at=installed_at,
        uninstalled_at=uninstalled_at,
        data_purge_at=data_purge_at,
        grace_period_ends_at=grace_period_ends_at,
    )


@pytest_asyncio.fixture
async def persisted_shop(db_session):
    """A Growth/active shop already saved to the in-memory DB."""
    shop = make_shop()
    db_session.add(shop)
    await db_session.commit()
    await db_session.refresh(shop)
    return shop


# ── Usage log helper ──────────────────────────────────────────────────────────

async def add_usage(
    db: AsyncSession,
    shop: Shop,
    generations: int,
    action: str = "gift_search",
    created_at: datetime | None = None,
    model: str = "claude-haiku-4-5",
):
    """Insert a UsageLog row for the given shop.

    Pass `created_at` explicitly to test cycle-boundary filtering — otherwise
    the row lands at the current time (inside the billing cycle for most tests).
    Pass `model` to exercise the generation-history model filter.
    """
    log = UsageLog(
        id=uuid.uuid4(),
        shop_id=shop.id,
        action_type=action,
        generations_consumed=generations,
        tokens_input=1000,
        tokens_output=500,
        model_used=model,
        cost_usd=0.01,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(log)
    await db.commit()
    return log


# ── Webhook HMAC helper ───────────────────────────────────────────────────────

def make_webhook_headers(body: bytes, secret: str = TEST_API_SECRET, webhook_id: str | None = None) -> dict:
    """Generate valid Shopify webhook headers for a given body."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    signature = base64.b64encode(digest).decode()
    return {
        "x-shopify-hmac-sha256": signature,
        "x-shopify-webhook-id": webhook_id or str(uuid.uuid4()),
        "x-shopify-topic": "app_subscriptions/update",
        "x-shopify-shop-domain": TEST_SHOP_DOMAIN,
    }


# ── No real network in tests ──────────────────────────────────────────────────

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", b"127.0.0.1", b"::1", b"localhost"}


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """Fail loudly on any outbound connection. Tests must mock Shopify, LLM and
    other HTTP calls; an unmocked call used to reach the real internet silently
    (and hang on a flaky connection). Loopback stays allowed."""
    import socket

    real_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host not in _LOCAL_HOSTS:
            raise RuntimeError(f"Network access in tests is blocked (tried to resolve {host!r}); mock the call.")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


# ── Settings override ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def patch_settings(monkeypatch):
    """Override settings for every test — no real secrets needed."""
    from core import config as core_config
    monkeypatch.setattr(core_config.settings, "shopify_api_secret", TEST_API_SECRET)
    monkeypatch.setattr(core_config.settings, "token_encryption_key", TEST_ENCRYPTION_KEY)
    monkeypatch.setattr(core_config.settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(core_config.settings, "anthropic_api_key", "sk-ant-test")
    monkeypatch.setattr(core_config.settings, "app_env", "test")
    # /billing/callback retries an unconfirmed subscription lookup; no real sleeps in tests.
    monkeypatch.setattr("app.routes.billing._SUBSCRIPTION_LOOKUP_DELAY_SECS", 0)
