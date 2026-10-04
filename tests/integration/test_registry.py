"""Gift registries (Pro) through the App Proxy: owner adds and manages items,
guests buy from the shared page, orders/create counts purchases. The owner
is only ever Shopify's signed logged_in_customer_id."""
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.services import registry as reg
from app.services.gifting.embeddings import FakeEmbedder
from core.db.models import GiftOrder, Registry, RegistryItem, UsageLog
from tests.integration.test_gift_order_webhook import post
from tests.integration.test_playground import picking_chat, row
from tests.integration.test_storefront import call, proxy_params, seeded_shop
from tests.unit.test_gift_orders_parse import line, order

OWNER = "7001"


def as_owner(db, method, path, customer=OWNER, **kw):
    return call(db, method, path, params=proxy_params(logged_in_customer_id=customer), **kw)


def add(db, product_id="1", variant_id="111", customer=OWNER, **kw):
    return as_owner(db, "POST", "/api/storefront/registry/items", customer=customer,
                    json={"product_id": product_id, "variant_id": variant_id, "variant_title": "Large", **kw})


async def pro_shop(db, plan="pro"):
    return await seeded_shop(db, plan_tier=plan)


async def the_registry(db) -> Registry:
    return (await db.execute(select(Registry))).scalar_one()


# ── Gating ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("plan,on", [("growth", False), ("pro", True)])
async def test_config_flag(db_session, plan, on):
    await pro_shop(db_session, plan)
    assert call(db_session, "GET", "/api/storefront/config").json()["registry"] is on


@pytest.mark.asyncio
async def test_must_be_logged_in_and_on_pro(db_session):
    await pro_shop(db_session)
    assert add(db_session, customer="").status_code == 401


@pytest.mark.asyncio
async def test_growth_cant_add(db_session):
    await pro_shop(db_session, "growth")
    assert add(db_session).status_code == 403


# ── Owner ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_first_add_creates_the_registry_from_catalog_data(db_session):
    await pro_shop(db_session)
    resp = add(db_session)
    assert resp.status_code == 200 and resp.json()["count"] == 1
    assert add(db_session, quantity=2).json()["count"] == 1            # same variant tops up
    add(db_session, variant_id="112")
    r = await the_registry(db_session)
    assert r.customer_id == OWNER and len(r.share_token) >= 16
    items = (await db_session.execute(select(RegistryItem).order_by(RegistryItem.variant_id))).scalars().all()
    assert [(i.variant_id, i.wanted_qty) for i in items] == [("111", 3), ("112", 1)]
    assert items[0].title == "Candle 1" and items[0].variant_title == "Large"   # title from our catalog


@pytest.mark.asyncio
async def test_unknown_or_excluded_products_are_refused(db_session):
    shop = await pro_shop(db_session)
    db_session.add(row(shop, "99", excluded=True))
    await db_session.commit()
    assert add(db_session, product_id="99").status_code == 422
    assert add(db_session, product_id="12345").status_code == 422


@pytest.mark.asyncio
async def test_owner_updates_details_and_items(db_session):
    await pro_shop(db_session)
    add(db_session)
    add(db_session, variant_id="112")
    resp = as_owner(db_session, "POST", "/api/storefront/registry",
                    json={"title": "  Sam & Alex  ", "occasion": "wedding", "event_date": "2027-06-12"})
    assert resp.status_code == 200
    r = await the_registry(db_session)
    await db_session.refresh(r)
    assert (r.title, r.occasion, r.event_date.isoformat()) == ("Sam & Alex", "wedding", "2027-06-12")
    assert as_owner(db_session, "POST", "/api/storefront/registry", json={"occasion": "funeral"}).status_code == 422

    items = (await db_session.execute(select(RegistryItem).order_by(RegistryItem.variant_id))).scalars().all()
    as_owner(db_session, "POST", f"/api/storefront/registry/items/{items[0].id}", json={"wanted_qty": 4})
    as_owner(db_session, "POST", f"/api/storefront/registry/items/{items[1].id}", json={"wanted_qty": 0})
    left = (await db_session.execute(select(RegistryItem))).scalars().all()
    await db_session.refresh(left[0])
    assert [(i.variant_id, i.wanted_qty) for i in left] == [("111", 4)]


@pytest.mark.asyncio
async def test_someone_else_cant_touch_my_items(db_session):
    await pro_shop(db_session)
    add(db_session)
    item = (await db_session.execute(select(RegistryItem))).scalar_one()
    resp = as_owner(db_session, "POST", f"/api/storefront/registry/items/{item.id}", customer="8002",
                    json={"wanted_qty": 0})
    assert resp.status_code == 404
    assert (await db_session.execute(select(RegistryItem))).scalar_one() is not None


@pytest.mark.asyncio
async def test_owner_page(db_session):
    await pro_shop(db_session)
    add(db_session)
    as_owner(db_session, "POST", "/api/storefront/registry",
             json={"title": "{{ shop.secret }} <script>alert(1)</script>"})
    resp = as_owner(db_session, "GET", "/api/storefront/registry")
    page = resp.text
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/liquid")
    assert "{{ shop.secret }}" not in page and "<script>alert" not in page        # no Liquid or HTML injection
    assert "&#123;&#123; shop.secret &#125;&#125;" in page
    r = await the_registry(db_session)
    assert "{{ shop.url }}/apps/giftsense/registry/" + r.share_token in page
    assert "{{ 3000 | money }}" in page and "Candle 1" in page
    assert "layout none" not in page                                               # rendered inside the theme

    anon = call(db_session, "GET", "/api/storefront/registry")
    assert "Log in" in anon.text and "Candle 1" not in anon.text


def test_page_scripts_leave_no_stray_liquid():
    """Only the money filter and shop.url may produce {{ }} in our pages."""
    import re
    src = open("app/routes/registry.py").read()
    assert not re.search(r"\{%", src.replace("{%%", ""))


# ── Guests ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_guest_page(db_session):
    await pro_shop(db_session)
    add(db_session)
    add(db_session, variant_id="112")
    r = await the_registry(db_session)
    items = (await db_session.execute(select(RegistryItem).order_by(RegistryItem.variant_id))).scalars().all()
    items[1].bought_qty = 1
    await db_session.commit()

    resp = call(db_session, "GET", f"/api/storefront/registry/{r.share_token}")
    page = resp.text
    assert resp.status_code == 200 and resp.headers["x-robots-tag"] == "noindex, nofollow"
    assert f'data-line="{r.share_token}:{items[0].id}"' in page and 'data-add="111"' in page
    assert 'data-add="112"' not in page and "All bought" in page
    assert OWNER not in page                                                        # never the owner's id
    await db_session.refresh(r)
    assert r.views == 1
    assert call(db_session, "GET", "/api/storefront/registry/not-a-token").status_code == 404


@pytest.mark.asyncio
async def test_guest_page_off_when_shop_leaves_pro(db_session):
    shop = await pro_shop(db_session)
    add(db_session)
    r = await the_registry(db_session)
    shop.plan_tier = "growth"
    await db_session.commit()
    assert call(db_session, "GET", f"/api/storefront/registry/{r.share_token}").status_code == 404


@pytest.mark.asyncio
async def test_orders_count_registry_purchases(db_session, job_pool):
    shop = await pro_shop(db_session)
    add(db_session)
    r = await the_registry(db_session)
    item = (await db_session.execute(select(RegistryItem))).scalar_one()
    payload = order([line("40.00", qty=2, props={"_giftsense_registry": f"{r.share_token}:{item.id}", "Registry": "x"}),
                     line("10.00", props={"_giftsense_registry": f"forged:{uuid.uuid4()}"})])
    assert post(db_session, "orders/create", payload).status_code == 200
    await db_session.refresh(item)
    assert item.bought_qty == 2
    gift = (await db_session.execute(select(GiftOrder))).scalar_one()
    assert gift.registry_id == r.id and float(gift.registry_revenue) == 80.0 and gift.gift_lines == 2


def test_registry_line_parsing():
    tid = uuid.uuid4()
    assert reg.parse_line(f"tok:{tid}") == ("tok", tid)
    assert reg.parse_line("tok") is None and reg.parse_line(f":{tid}") is None and reg.parse_line(None) is None


# ── AI suggestions ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_suggestions_are_metered_and_cached_for_the_day(db_session):
    await pro_shop(db_session)
    add(db_session)
    with patch("app.routes.registry.chat", picking_chat()), \
         patch("app.routes.registry.OpenAIEmbedder", return_value=FakeEmbedder()):
        first = as_owner(db_session, "POST", "/api/storefront/registry/suggestions").json()["picks"]
        again = as_owner(db_session, "POST", "/api/storefront/registry/suggestions").json()["picks"]
    assert first and first == again
    assert "1" not in {p["product_id"] for p in first}                      # already on the registry
    logs = (await db_session.execute(select(UsageLog).where(UsageLog.action_type == "registry_suggest"))).scalars().all()
    assert sum(log.generations_consumed for log in logs) == 1              # one search charged; the cached call was free


def test_suggestion_intake():
    r = Registry(occasion="wedding")
    items = [RegistryItem(product_id=str(i), title=f"Item {i}", price=p) for i, p in enumerate([30, 60, 80])]
    intake = reg.suggestion_intake(r, items)
    assert (intake.recipient, intake.occasion, intake.budget_band) == ("partner", "wedding", "50_100")
    assert intake.exclude_ids == ["0", "1", "2"] and "Item 0" in intake.free_text
    assert reg.cached_suggestions(Registry(suggestions={"day": "2000-01-01", "picks": [1]})) is None


# ── Privacy ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_customers_redact_and_data_request(db_session):
    from app.services.gdpr import collect_customer_data, redact_customer
    shop = await pro_shop(db_session)
    add(db_session)
    add(db_session, customer="8002")
    data = await collect_customer_data(shop.id, {"customer": {"id": int(OWNER)}}, db_session)
    assert [r["customer_id"] for r in data["registries"]] == [OWNER] and len(data["registry_items"]) == 1
    counts = await redact_customer(shop.id, {"customer": {"id": int(OWNER)}, "orders_to_redact": []}, db_session)
    assert counts["registries"] == 1
    assert [r.customer_id for r in (await db_session.execute(select(Registry))).scalars()] == ["8002"]
    assert len((await db_session.execute(select(RegistryItem))).scalars().all()) == 1


# ── Dashboard ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dashboard_overview(db_session, job_pool):
    from tests.integration.test_media import dashboard_get
    await pro_shop(db_session)
    add(db_session)
    add(db_session, variant_id="112", quantity=2)
    r = await the_registry(db_session)
    item = (await db_session.execute(select(RegistryItem).where(RegistryItem.variant_id == "111"))).scalar_one()
    call(db_session, "GET", f"/api/storefront/registry/{r.share_token}")
    post(db_session, "orders/create", order([line("40.00", props={"_giftsense_registry": f"{r.share_token}:{item.id}"})]))
    data = dashboard_get(db_session, "/api/registries").json()
    assert data["available"] is True
    assert data["totals"] == {"registries": 1, "items": 2, "wanted": 3, "bought": 1, "views": 1, "orders": 1,
                              "revenue": 40.0, "currency": "USD"}
    assert data["registries"][0]["items"] == 2 and data["registries"][0]["bought"] == 1
    assert OWNER not in json.dumps(data)                                      # never the customer id
