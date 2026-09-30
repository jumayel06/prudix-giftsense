"""POST /api/storefront/note/draft: metered AI gift-note drafts via the App Proxy."""
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.config import PLANS
from app.llm import LLMResponse
from core.db.models import GiftSession, UsageLog
from tests.conftest import add_usage
from tests.integration.test_storefront import call, seeded_shop

SID = str(uuid.uuid4())
BODY = {"sid": SID, "recipient": "parent", "occasion": "birthday", "product_id": "1"}


def note_chat(text="Happy birthday, Mom! Enjoy the calm evenings."):
    return AsyncMock(return_value=LLMResponse(json.dumps({"note": text}), 300, 50, model="gpt-6-luna"))


@pytest.fixture
def note_ai():
    chat = note_chat()
    with patch("app.routes.storefront.chat", chat), \
         patch("app.routes.storefront.moderate", AsyncMock(return_value=False)):
        yield chat


async def used(db, shop):
    return int((await db.execute(select(func.coalesce(func.sum(UsageLog.generations_consumed), 0))
                                 .where(UsageLog.shop_id == shop.id))).scalar())


@pytest.mark.asyncio
async def test_draft_uses_product_context_and_meters(db_session, note_ai):
    shop = await seeded_shop(db_session, plan_tier="growth", selected_model="advanced")
    resp = call(db_session, "POST", "/api/storefront/note/draft", json={**BODY, "name": "Mom"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["note"].startswith("Happy birthday") and data["source"] == "ai"
    assert data["rewrites_left"] == 3 and data["max_chars"] == 250
    assert "Candle 1" in note_ai.await_args.kwargs["prompt"] and "Mom" in note_ai.await_args.kwargs["prompt"]
    assert await used(db_session, shop) == 2          # Advanced weight
    action = (await db_session.execute(select(UsageLog.action_type))).scalars().first()
    assert action == "gift_note"


@pytest.mark.asyncio
async def test_merchant_settings_shape_the_draft(db_session, note_ai):
    shop = await seeded_shop(db_session)
    shop.gift_settings = {"notes": {"tone": "formal", "max_chars": 120, "banned_words": ["cheap"]}}
    await db_session.commit()
    data = call(db_session, "POST", "/api/storefront/note/draft", json=BODY).json()
    assert data["max_chars"] == 120 and data["tone"] == "formal"
    assert "formal" in note_ai.await_args.kwargs["system"].lower() and "cheap" in note_ai.await_args.kwargs["prompt"]


@pytest.mark.asyncio
async def test_shopper_can_pick_a_tone(db_session, note_ai):
    await seeded_shop(db_session)
    data = call(db_session, "POST", "/api/storefront/note/draft", json={**BODY, "tone": "playful"}).json()
    assert data["tone"] == "playful"


@pytest.mark.asyncio
async def test_context_falls_back_to_the_gift_session(db_session, note_ai):
    shop = await seeded_shop(db_session)
    db_session.add(GiftSession(shop_id=shop.id, sid=uuid.UUID(SID), intake={"recipient": "teacher", "occasion": "thank_you"},
                               last_picks=[], searches=1))
    await db_session.commit()
    call(db_session, "POST", "/api/storefront/note/draft", json={"sid": SID})
    prompt = note_ai.await_args.kwargs["prompt"]
    assert "Teacher" in prompt and "Thank you" in prompt


@pytest.mark.asyncio
async def test_three_rewrites_per_gift_then_409(db_session, note_ai):
    await seeded_shop(db_session)
    codes = [call(db_session, "POST", "/api/storefront/note/draft", json=BODY).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 200, 409]
    # A different gift has its own allowance.
    assert call(db_session, "POST", "/api/storefront/note/draft", json={**BODY, "product_id": "2"}).status_code == 200


@pytest.mark.asyncio
async def test_last_draft_is_kept_for_the_acceptance_metric(db_session, note_ai):
    shop = await seeded_shop(db_session)
    call(db_session, "POST", "/api/storefront/note/draft", json=BODY)
    s = (await db_session.execute(select(GiftSession).where(GiftSession.shop_id == shop.id))).scalar_one()
    assert s.note_drafts["1"]["last"].startswith("Happy birthday") and s.note_drafts["1"]["count"] == 1


@pytest.mark.asyncio
async def test_over_limit_returns_a_template_without_ai(db_session, note_ai):
    shop = await seeded_shop(db_session, plan_tier="starter", selected_model="standard")
    await add_usage(db_session, shop, PLANS["starter"]["generation_limit"])
    data = call(db_session, "POST", "/api/storefront/note/draft", json=BODY).json()
    assert data["source"] == "template" and data["note"]
    note_ai.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_failure_is_refunded(db_session):
    shop = await seeded_shop(db_session)
    with patch("app.routes.storefront.chat", AsyncMock(side_effect=RuntimeError("down"))), \
         patch("app.routes.storefront.moderate", AsyncMock(return_value=False)):
        data = call(db_session, "POST", "/api/storefront/note/draft", json=BODY).json()
    assert data["source"] == "template" and await used(db_session, shop) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{"recipient": "alien"}, {"tone": "sarcastic"}, {"name": "x" * 41},
                                 {"name": "<script>"}, {"sid": "nope"}])
async def test_input_is_validated(db_session, note_ai, bad):
    await seeded_shop(db_session)
    assert call(db_session, "POST", "/api/storefront/note/draft", json={**BODY, **bad}).status_code == 422
