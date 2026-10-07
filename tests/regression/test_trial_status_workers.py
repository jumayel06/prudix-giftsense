"""Background jobs must include trial shops (plan_status "trial_active") with
active ones, and skip pending / uninstalled / purged ones.

Ported from Prudix Commerce, where several workers queried plan_status="trial"
(a value that's never stored) and silently skipped every trial shop. Covers
GiftSense's shop-selecting jobs: catalog sync kicks/reconcile, the weekly
email, and a static guard against the "trial" typo anywhere in app/.
"""
import pathlib
import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.services import catalog_sync
from core.db.models import GiftOrder
from tests.conftest import make_shop

STATUSES = ["active", "trial_active", "pending", "uninstalled", "purged", "cancelled"]


async def seed(db, tier="pro"):
    shops = {}
    for i, status in enumerate(STATUSES):
        s = make_shop(plan_tier=tier, plan_status=status, domain=f"s-{status}-{i}.myshopify.com")
        s.shop_owner_email = f"{status}@x.co"
        db.add(s)
        shops[status] = s
    await db.commit()
    return shops


def test_no_job_or_route_queries_the_nonexistent_trial_status():
    pattern = re.compile(r"""plan_status\s*(==|!=)\s*["']trial["']|["']trial["']\s*[,)\]]""")
    hits = [f"{p}:{n}" for p in pathlib.Path("app").rglob("*.py")
            for n, line in enumerate(p.read_text().splitlines(), 1) if pattern.search(line)]
    assert hits == [], f"use 'trial_active', not 'trial': {hits}"


def test_catalog_jobs_cover_active_and_trial_shops():
    assert set(catalog_sync.ACTIVE_STATUSES) == {"active", "trial_active"}


@pytest.mark.asyncio
async def test_catalog_kick_selects_active_and_trial_shops(db_session):
    from app.workers.catalog import _active_shop_ids
    shops = await seed(db_session)
    picked = {domain for _, domain in await _active_shop_ids(db_session)}
    assert picked == {shops["active"].shop_domain, shops["trial_active"].shop_domain}


@pytest.mark.asyncio
async def test_weekly_email_reaches_active_and_trial_shops_only(db_session):
    from app.workers.digest import send_weekly_digests
    shops = await seed(db_session, tier="growth")
    for s in shops.values():   # some activity so nobody is skipped as "quiet"
        db_session.add(GiftOrder(shop_id=s.id, order_id=f"o-{s.id}", order_name="#1", gift_lines=1, gift_revenue=10,
                                 order_total=10, currency="USD", groups=[]))
    await db_session.commit()
    send = AsyncMock(return_value={})
    with patch("app.workers.digest.AsyncSessionLocal") as sess, patch("app.workers.digest.send_email", send):
        sess.return_value.__aenter__.return_value = db_session
        sess.return_value.__aexit__.return_value = False
        await send_weekly_digests({}, datetime.now(timezone.utc))
    assert {c.kwargs["to_email"] for c in send.await_args_list} == {"active@x.co", "trial_active@x.co"}
