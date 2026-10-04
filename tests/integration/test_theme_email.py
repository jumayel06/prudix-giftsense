"""themes/publish → check_theme emails the owner once when the new live theme
doesn't have GiftSense switched on (app/workers/theme.py)."""
from unittest.mock import AsyncMock, patch

import pytest

from app.services import theme_status
from tests.conftest import TEST_SHOP_DOMAIN, make_shop


async def run_check(db_session, result):
    from app.workers.theme import check_theme
    send = AsyncMock(return_value={"message_id": "x"})
    with patch("app.workers.theme.AsyncSessionLocal") as sess, \
            patch("app.workers.theme.get_valid_access_token", AsyncMock(return_value="tok")), \
            patch("app.workers.theme.theme_status.fetch_embed_status", AsyncMock(return_value=result)), \
            patch("app.workers.theme.send_email", send):
        sess.return_value.__aenter__.return_value = db_session
        sess.return_value.__aexit__.return_value = False
        await check_theme({}, TEST_SHOP_DOMAIN)
    return send


def r(embed, theme, blocks=None):
    return {"embed": embed, "theme_name": theme, "blocks": blocks or {}}


@pytest.mark.asyncio
async def test_new_theme_without_the_finder_emails_once(db_session):
    shop = make_shop(plan_tier="starter")
    shop.shop_owner_email = "owner@snow.co"
    db_session.add(shop)
    await db_session.commit()

    assert (await run_check(db_session, r("on", "Dawn", {"Home page": ["Find a gift"]}))).await_count == 0
    send = await run_check(db_session, r("off", "Sense"))
    assert send.await_count == 1
    kw = send.await_args.kwargs
    assert kw["to_email"] == "owner@snow.co" and kw["subject"] == "The gift finder is off on Sense"
    assert "Find a gift and Gift options blocks" in kw["text_body"]                 # sections were lost too
    assert (await run_check(db_session, r("off", "Sense"))).await_count == 0        # same theme: no repeat
    assert (await run_check(db_session, r("missing", "Craft"))).await_count == 1    # another switch: new email


@pytest.mark.asyncio
async def test_no_email_on_first_install_or_without_owner_email(db_session):
    shop = make_shop()
    db_session.add(shop)
    await db_session.commit()
    assert (await run_check(db_session, r("off", "Dawn"))).await_count == 0          # never had it: onboarding, not an alert
    await run_check(db_session, r("on", "Dawn"))
    assert (await run_check(db_session, r("off", "Sense"))).await_count == 0         # no owner email
    assert theme_status.warning(shop)["theme_changed"] is True
