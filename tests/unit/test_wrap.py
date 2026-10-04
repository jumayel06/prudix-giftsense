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
            return resp({"publications": {"nodes": [{"id": "gid://shopify/Publication/5", "catalog": {"title": "Online Store"}},
                                                   {"id": "gid://shopify/Publication/6", "catalog": {"title": "Point of Sale"}}]}})
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
        resp({"publications": {"nodes": [{"id": "gid://shopify/Publication/5", "catalog": {"title": "Online Store"}}]}}),
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
    assert wrap.storefront_styles(shop) == [{"variant_id": 1, "name": "Gold", "price": 5.0, "kind": "wrap"}]
    shop.gift_settings["wrap"]["ready"] = False
    assert wrap.storefront_styles(shop) == []


# ── Style photos ────────────────────────────────────────────────────────────

STAGED = wrap.STAGED_HOST + "tmp/1/products/abc/gold.jpg"
SYNCED_PHOTO = {"name": "Gold", "price": 5.0, "variant_id": "gid://shopify/ProductVariant/1", "image_source": STAGED,
                "file_id": "gid://shopify/MediaImage/7", "image_url": "https://cdn.shopify.com/gold.jpg"}


def _shop_with(style):
    shop = make_shop()
    shop.gift_settings = {"wrap": {"enabled": True, "product_id": "p", "ready": True, "styles": [dict(style)]}}
    return shop


def test_new_photo_needs_a_resync():
    shop = _shop_with({"name": "Gold", "price": 5.0, "variant_id": "v1"})
    wrap.update_wrap_settings(shop, wrap.WrapUpdate(styles=[{"name": "Gold", "price": 5, "image": STAGED}]))
    s = wrap.wrap_settings(shop)
    assert s["styles"][0]["image_source"] == STAGED and s["ready"] is False


def test_keeping_the_photo_keeps_the_product_ready():
    shop = _shop_with(SYNCED_PHOTO)
    wrap.update_wrap_settings(shop, wrap.WrapUpdate(styles=[{"name": "Gold", "price": 5,
                                                             "image": SYNCED_PHOTO["image_url"]}]))
    s = wrap.wrap_settings(shop)
    assert s["ready"] is True and s["styles"][0]["file_id"] == "gid://shopify/MediaImage/7"


def test_removing_the_photo_needs_a_resync():
    shop = _shop_with(SYNCED_PHOTO)
    wrap.update_wrap_settings(shop, wrap.WrapUpdate(styles=[{"name": "Gold", "price": 5, "image": None}]))
    s = wrap.wrap_settings(shop)
    assert s["ready"] is False and not any(k in s["styles"][0] for k in wrap.IMAGE_FIELDS)


def test_photo_urls_from_elsewhere_are_refused():
    shop = _shop_with({"name": "Gold", "price": 5.0})
    with pytest.raises(ValueError):
        wrap.update_wrap_settings(shop, wrap.WrapUpdate(styles=[{"name": "Gold", "price": 5,
                                                                 "image": "https://evil.example/x.jpg"}]))


def _photo_gql(media_urls):
    """productSet → publications → publish → variant media reads (one url per poll)."""
    calls = []
    urls = iter(media_urls)

    async def gql(shop, token, query, variables=None):
        calls.append((query, variables))
        if "productSet" in query:
            return resp({"productSet": {"product": {"id": "gid://shopify/Product/9", "variants": {"nodes": [
                {"id": "gid://shopify/ProductVariant/1", "title": "Gold"},
                {"id": "gid://shopify/ProductVariant/2", "title": "Kraft"}]}}, "userErrors": []}})
        if "publications" in query:
            return resp({"publications": {"nodes": [{"id": "gid://shopify/Publication/5", "catalog": {"title": "Online Store"}}]}})
        if "publishablePublish" in query:
            return resp({"publishablePublish": {"userErrors": []}})
        return resp({"product": {"variants": {"nodes": [
            {"id": "gid://shopify/ProductVariant/1", "media": {"nodes": [
                {"id": "gid://shopify/MediaImage/7", "image": ({"url": u} if (u := next(urls)) else None)}]}},
            {"id": "gid://shopify/ProductVariant/2", "media": {"nodes": []}}]}}})
    return gql, calls


@pytest.mark.asyncio
async def test_sync_attaches_photos_and_reads_back_the_cdn_url(monkeypatch):
    monkeypatch.setattr(wrap, "IMAGE_POLL_SECS", 0)
    gql, calls = _photo_gql([None, "https://cdn.shopify.com/gold.jpg"])   # processing, then ready
    settings = {"enabled": True, "styles": [{"name": "Gold", "price": 5.0, "image_source": STAGED},
                                            {"name": "Kraft", "price": 3.0}]}
    out = await wrap.sync_wrap_product("s", "t", settings, gql=gql)
    inp = calls[0][1]["input"]
    photo = {"originalSource": STAGED, "contentType": "IMAGE", "alt": "Gold gift wrap"}
    assert inp["files"] == [photo] and inp["variants"][0]["file"] == photo and "file" not in inp["variants"][1]
    gold, kraft = out["styles"]
    assert out["ready"] is True
    assert gold["file_id"] == "gid://shopify/MediaImage/7" and gold["image_url"] == "https://cdn.shopify.com/gold.jpg"
    assert "image_url" not in kraft


@pytest.mark.asyncio
async def test_resync_reuses_the_uploaded_file():
    gql, calls = _photo_gql(["https://cdn.shopify.com/gold.jpg"])
    await wrap.sync_wrap_product("s", "t", {"enabled": True, "product_id": "gid://shopify/Product/9",
                                            "styles": [dict(SYNCED_PHOTO)]}, gql=gql)
    inp = calls[0][1]["input"]
    assert inp["files"] == [{"id": "gid://shopify/MediaImage/7"}]   # staged URLs expire; the file id doesn't


@pytest.mark.asyncio
async def test_sync_without_photos_clears_product_media_and_skips_the_read():
    gql, calls = _photo_gql([])
    await wrap.sync_wrap_product("s", "t", {"enabled": True, "styles": [{"name": "Gold", "price": 5.0}]}, gql=gql)
    assert calls[0][1]["input"]["files"] == [] and len(calls) == 3


def test_storefront_styles_include_the_photo():
    shop = _shop_with(SYNCED_PHOTO)
    assert wrap.storefront_styles(shop) == [{"variant_id": 1, "name": "Gold", "price": 5.0, "kind": "wrap",
                                             "image": "https://cdn.shopify.com/gold.jpg"}]


@pytest.mark.asyncio
async def test_image_upload_target():
    gql = AsyncMock(return_value=resp({"stagedUploadsCreate": {"userErrors": [], "stagedTargets": [{
        "url": "https://shopify-staged-uploads.storage.googleapis.com/", "resourceUrl": STAGED,
        "parameters": [{"name": "key", "value": "tmp/1/gold.jpg"}]}]}}))
    out = await wrap.create_image_upload("s", "t", "gold.jpg", "image/jpeg", 1000, gql=gql)
    assert out["resource_url"] == STAGED and out["parameters"] == [{"name": "key", "value": "tmp/1/gold.jpg"}]
    sent = gql.await_args.args[3]["input"][0]
    assert sent["resource"] == "IMAGE" and sent["fileSize"] == "1000"


@pytest.mark.asyncio
@pytest.mark.parametrize("mime,size", [("image/svg+xml", 10), ("application/pdf", 10), ("image/png", 0),
                                       ("image/png", wrap.MAX_IMAGE_BYTES + 1)])
async def test_image_upload_rejects_bad_files(mime, size):
    gql = AsyncMock()
    with pytest.raises(ValueError):
        await wrap.create_image_upload("s", "t", "x", mime, size, gql=gql)
    gql.assert_not_awaited()


# ── Types and products that can't be wrapped ────────────────────────────────

def test_style_type_is_a_label_change_without_resync():
    shop = _shop_with({"name": "Gold", "price": 5.0, "variant_id": "gid://shopify/ProductVariant/1"})
    wrap.update_wrap_settings(shop, wrap.WrapUpdate(styles=[{"name": "Gold", "price": 5, "kind": "bag"}]))
    s = wrap.wrap_settings(shop)
    assert s["ready"] is True and s["styles"][0]["kind"] == "bag"
    assert wrap.storefront_styles(shop)[0]["kind"] == "bag"
    with pytest.raises(ValidationError):
        wrap.WrapUpdate(styles=[{"name": "Gold", "price": 5, "kind": "ribbon"}])


@pytest.mark.parametrize("tags,ok", [([], True), (["sale"], True), (["No-Gift-Wrap"], False), ([" no-gift-wrap "], False)])
def test_wrappable(tags, ok):
    assert wrap.wrappable(tags) is ok


def test_greeting_cards_have_their_own_limit():
    ok = [{"name": f"W{i}", "price": 1} for i in range(3)] + [{"name": f"C{i}", "price": 2, "kind": "card"} for i in range(2)]
    assert len(wrap.WrapUpdate(styles=ok).styles) == 5
    with pytest.raises(ValidationError):
        wrap.WrapUpdate(styles=ok + [{"name": "C3", "price": 2, "kind": "card"}])
    with pytest.raises(ValidationError):
        wrap.WrapUpdate(styles=[{"name": f"W{i}", "price": 1} for i in range(4)])
