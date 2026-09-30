"""Gift wrap: merchant styles → one hidden 'Gift wrap' product (app/services/wrap.py)."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.services import wrap
from tests.conftest import make_shop


def resp(data, status=200):
    r = MagicMock(status_code=status)
    r.json.return_value = {"data": data}
    return r


def test_defaults_are_off():
    assert wrap.wrap_settings(make_shop()) == {"enabled": False, "styles": [], "product_id": None, "ready": False,
                                               "error": None}


def test_update_validates_and_keeps_variant_ids_for_unchanged_styles():
    shop = make_shop()
    shop.gift_settings = {"wrap": {"enabled": True, "product_id": "gid://shopify/Product/9", "ready": True,
                                   "styles": [{"name": "Gold", "price": 5.0, "variant_id": "gid://shopify/ProductVariant/1"}]}}
    wrap.update_wrap_settings(shop, wrap.WrapUpdate(styles=[{"name": " Gold ", "price": 6}, {"name": "Kraft", "price": 3}]))
    s = wrap.wrap_settings(shop)
    assert [x["name"] for x in s["styles"]] == ["Gold", "Kraft"] and s["styles"][0]["price"] == 6.0
    assert s["ready"] is False                         # needs a re-sync after edits


def test_saving_the_same_styles_keeps_the_product_ready():
    shop = make_shop()
    shop.gift_settings = {"wrap": {"enabled": True, "product_id": "p", "ready": True,
                                   "styles": [{"name": "Gold", "price": 5.0, "variant_id": "v1"}]}}
    wrap.update_wrap_settings(shop, wrap.WrapUpdate(enabled=True, styles=[{"name": "Gold", "price": "5.00"}]))
    assert wrap.wrap_settings(shop)["ready"] is True


@pytest.mark.parametrize("styles", [
    [{"name": "A", "price": 1}] * 4,                    # max 3
    [{"name": "", "price": 1}],
    [{"name": "A", "price": -1}],
    [{"name": "A", "price": 1}, {"name": "a", "price": 2}],   # duplicate names
    [{"name": "x" * 41, "price": 1}],
])
def test_invalid_styles(styles):
    with pytest.raises(ValidationError):
        wrap.WrapUpdate(styles=styles)


@pytest.mark.asyncio
async def test_sync_creates_an_unlisted_product_and_publishes_it():
    calls = []

    async def gql(shop, token, query, variables=None):
        calls.append((query, variables))
        if "productSet" in query:
            return resp({"productSet": {"product": {"id": "gid://shopify/Product/9", "variants": {"nodes": [
                {"id": "gid://shopify/ProductVariant/1", "title": "Gold"},
                {"id": "gid://shopify/ProductVariant/2", "title": "Kraft"}]}}, "userErrors": []}})
        if "publications" in query:
            return resp({"publications": {"nodes": [{"id": "gid://shopify/Publication/5", "name": "Online Store"},
                                                   {"id": "gid://shopify/Publication/6", "name": "Point of Sale"}]}})
        return resp({"publishablePublish": {"userErrors": []}})

    settings = {"enabled": True, "styles": [{"name": "Gold", "price": 5.0}, {"name": "Kraft", "price": 3.0}]}
    out = await wrap.sync_wrap_product("shop.myshopify.com", "tok", settings, gql=gql)
    assert out["ready"] is True and out["product_id"] == "gid://shopify/Product/9"
    assert [s["variant_id"] for s in out["styles"]] == ["gid://shopify/ProductVariant/1", "gid://shopify/ProductVariant/2"]

    product_set = calls[0][1]
    assert "identifier" not in product_set                      # first sync creates
    inp = product_set["input"]
    assert inp["status"] == "UNLISTED" and "giftsense-wrap" in inp["tags"]
    assert inp["productOptions"] == [{"name": "Style", "values": [{"name": "Gold"}, {"name": "Kraft"}]}]
    assert inp["variants"][0]["price"] == "5.00" and inp["variants"][0]["inventoryItem"] == {"tracked": False,
                                                                                              "requiresShipping": False}
    publish = calls[-1][1]
    assert publish["input"] == [{"publicationId": "gid://shopify/Publication/5"}]


@pytest.mark.asyncio
async def test_resync_updates_the_same_product():
    gql = AsyncMock(side_effect=[
        resp({"productSet": {"product": {"id": "gid://shopify/Product/9", "variants": {"nodes": [
            {"id": "gid://shopify/ProductVariant/1", "title": "Gold"}]}}, "userErrors": []}}),
        resp({"publications": {"nodes": [{"id": "gid://shopify/Publication/5", "name": "Online Store"}]}}),
        resp({"publishablePublish": {"userErrors": []}}),
    ])
    settings = {"enabled": True, "product_id": "gid://shopify/Product/9", "styles": [{"name": "Gold", "price": 5.0}]}
    await wrap.sync_wrap_product("s", "t", settings, gql=gql)
    assert gql.await_args_list[0].args[3]["identifier"] == {"id": "gid://shopify/Product/9"}


@pytest.mark.asyncio
async def test_sync_errors_are_reported_not_raised():
    gql = AsyncMock(return_value=resp({"productSet": {"product": None,
                                                      "userErrors": [{"field": ["title"], "message": "Nope"}]}}))
    out = await wrap.sync_wrap_product("s", "t", {"enabled": True, "styles": [{"name": "Gold", "price": 5}]}, gql=gql)
    assert out["ready"] is False and "Nope" in out["error"]


def test_storefront_styles_only_when_enabled_and_ready():
    shop = make_shop()
    shop.gift_settings = {"wrap": {"enabled": True, "ready": True, "product_id": "p",
                                   "styles": [{"name": "Gold", "price": 5.0, "variant_id": "gid://shopify/ProductVariant/1"}]}}
    assert wrap.storefront_styles(shop) == [{"variant_id": 1, "name": "Gold", "price": 5.0}]
    shop.gift_settings["wrap"]["ready"] = False
    assert wrap.storefront_styles(shop) == []
