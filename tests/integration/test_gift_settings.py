"""Merchant gift-note settings in GET/PUT /api/settings (tone, length, banned words)."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from core.db.session import get_db
from tests.conftest import TEST_SHOP_DOMAIN, make_shop


def call(db_session, method, **kw):
    async def override_db():
        yield db_session
    app.dependency_overrides[get_db] = override_db
    try:
        return TestClient(app).request(method, f"/api/settings?shop={TEST_SHOP_DOMAIN}", **kw)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_defaults(db_session):
    db_session.add(make_shop())
    await db_session.commit()
    notes = call(db_session, "GET").json()["gift_notes"]
    assert notes == {"tone": "warm", "max_chars": 250, "banned_words": []}
    assert {t["value"] for t in call(db_session, "GET").json()["note_tones"]} == {"warm", "elegant", "playful", "formal"}


@pytest.mark.asyncio
async def test_save_and_normalize(db_session):
    db_session.add(make_shop())
    await db_session.commit()
    resp = call(db_session, "PUT", json={"gift_notes": {"tone": "playful", "max_chars": 180,
                                                        "banned_words": [" Cheap ", "cheap", "", "discount"]}})
    assert resp.status_code == 200
    notes = call(db_session, "GET").json()["gift_notes"]
    assert notes == {"tone": "playful", "max_chars": 180, "banned_words": ["cheap", "discount"]}


@pytest.mark.asyncio
async def test_partial_update_keeps_other_fields(db_session):
    db_session.add(make_shop())
    await db_session.commit()
    call(db_session, "PUT", json={"gift_notes": {"tone": "formal"}})
    call(db_session, "PUT", json={"gift_notes": {"max_chars": 300}})
    assert call(db_session, "GET").json()["gift_notes"]["tone"] == "formal"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{"tone": "sarcastic"}, {"max_chars": 50}, {"max_chars": 900},
                                 {"banned_words": ["x"] * 51}, {"banned_words": ["y" * 41]}])
async def test_invalid_settings_rejected(db_session, bad):
    db_session.add(make_shop())
    await db_session.commit()
    assert call(db_session, "PUT", json={"gift_notes": bad}).status_code == 422
