"""Is the GiftSense app embed switched on in the live theme?

Reads config/settings_data.json of the MAIN theme (GraphQL, read_themes).
Lessons from Commerce: Shopify may suffix our app handle (-1, -2) in the theme
JSON, slashes can arrive escaped, and the file can start with a comment."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import theme_status as ts

HANDLE = "prudix-giftsense-dev"


def settings_data(blocks: dict, current_as_preset: bool = False, header: bool = True) -> str:
    body = {"current": "Default" if current_as_preset else {"blocks": blocks},
            "presets": {"Default": {"blocks": blocks}}}
    text = json.dumps(body)
    return ("/*\n * IMPORTANT: The contents of this file are auto-generated.\n */\n" if header else "") + text


def embed(handle=HANDLE, block="app-embed", disabled=False):
    return {"type": f"shopify://apps/{handle}/blocks/{block}/0f9a-uuid", "disabled": disabled, "settings": {}}


@pytest.mark.parametrize("blocks,expected", [
    ({"1": embed()}, "on"),
    ({"1": embed(disabled=True)}, "off"),
    ({}, "missing"),
    ({"1": embed(handle="some-other-app")}, "missing"),          # another app's embed
    ({"1": embed(block="gift-finder")}, "missing"),              # our section block isn't the embed
    ({"1": embed(handle=HANDLE + "-1")}, "on"),                  # Shopify's handle suffix
])
def test_embed_state(blocks, expected):
    assert ts.embed_state(settings_data(blocks), HANDLE) == expected


def test_escaped_slashes_and_preset_current():
    text = settings_data({"1": embed()}, current_as_preset=True).replace("/", "\\/")
    assert ts.embed_state(text, HANDLE) == "on"


def test_unparseable_file_is_unknown():
    assert ts.embed_state("{not json", HANDLE) == "unknown"


@pytest.mark.asyncio
async def test_fetch_uses_graphql_and_reports_the_theme():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"data": {
        "currentAppInstallation": {"app": {"handle": HANDLE}},
        "themes": {"nodes": [{"name": "Dawn", "files": {"nodes": [
            {"body": {"content": settings_data({"1": embed()})}}]}}]},
    }}
    gql = AsyncMock(return_value=resp)
    result = await ts.fetch_embed_status("shop.myshopify.com", "tok", gql=gql)
    assert result == {"embed": "on", "theme_name": "Dawn", "blocks": {"gift-finder": [], "gift-options": []}}
    assert "roles: [MAIN]" in gql.await_args.args[2] and "templates/*.json" in gql.await_args.args[2]


@pytest.mark.asyncio
async def test_fetch_failure_is_unknown_not_an_error():
    gql = AsyncMock(return_value=MagicMock(status_code=500))
    assert (await ts.fetch_embed_status("shop.myshopify.com", "tok", gql=gql))["embed"] == "unknown"


# ── Block placements (Find a gift section, Gift options) ─────────────────────

from app.services.theme_status import block_placements, page_label  # noqa: E402

FINDER = "shopify://apps/prudix-giftsense-dev/blocks/gift-finder/0199-aaaa"
OPTIONS = "shopify:\\/\\/apps\\/prudix-giftsense-dev-1\\/blocks\\/gift-options\\/0199-bbbb"   # escaped, suffixed


def tfile(name, data, comment=True):
    text = json.dumps(data)
    return {"filename": name, "body": {"content": ("/* auto-generated */\n" if comment else "") + text}}


def test_finds_blocks_by_page_and_skips_disabled():
    files = [
        tfile("templates/index.json", {"sections": {"apps_1": {"type": "apps", "blocks": {"b1": {"type": FINDER}}}}}),
        tfile("templates/product.json", {"sections": {"main": {"type": "main-product", "blocks": {
            "g": {"type": OPTIONS.replace("\\/", "/")}}}}}),
        tfile("templates/collection.json", {"sections": {"apps_1": {"type": "apps", "disabled": True,
                                                                   "blocks": {"b1": {"type": FINDER}}}}}),
        tfile("templates/cart.json", {"sections": {"apps_1": {"type": "apps", "blocks": {
            "b1": {"type": FINDER, "disabled": True}}}}}),
        tfile("templates/page.contact.json", {"sections": {}}),
    ]
    assert block_placements(files, "prudix-giftsense-dev") == {
        "gift-finder": ["Home page"], "gift-options": ["Product pages"]}


def test_other_apps_blocks_are_ignored():
    other = "shopify://apps/some-other-app/blocks/gift-finder/1"
    files = [tfile("templates/index.json", {"sections": {"a": {"type": "apps", "blocks": {"b": {"type": other}}}}})]
    assert block_placements(files, "prudix-giftsense-dev") == {"gift-finder": [], "gift-options": []}


@pytest.mark.parametrize("filename,label", [
    ("templates/index.json", "Home page"), ("templates/product.gift.json", "Product pages (gift)"),
    ("templates/page.holiday.json", "Pages (holiday)"), ("sections/header-group.json", "Header"),
    ("sections/footer-group.json", "Footer"),
])
def test_page_labels(filename, label):
    assert page_label(filename) == label


# ── Remembered checks → dashboard warning (theme changes) ────────────────────

from tests.conftest import make_shop  # noqa: E402


def result(embed, theme, finder=()):
    return {"embed": embed, "theme_name": theme, "blocks": {"gift-finder": list(finder), "gift-options": []}}


def test_no_warning_while_the_embed_is_on():
    shop = make_shop()
    ts.remember(shop, result("on", "Dawn", ["Home page"]))
    assert ts.warning(shop) is None


def test_switching_theme_warns_and_notes_lost_sections():
    shop = make_shop()
    ts.remember(shop, result("on", "Dawn", ["Home page"]))
    ts.remember(shop, result("missing", "Sense"))
    assert ts.warning(shop) == {"theme_name": "Sense", "theme_changed": True, "blocks_lost": True}
    ts.remember(shop, result("missing", "Sense"))                  # re-checked, still not fixed
    assert ts.warning(shop)["theme_changed"] is True and ts.warning(shop)["blocks_lost"] is True
    ts.remember(shop, result("on", "Sense"))                       # merchant switched it on
    assert ts.warning(shop) is None


def test_never_turned_on_is_a_plain_warning():
    shop = make_shop()
    ts.remember(shop, result("missing", "Dawn"))
    assert ts.warning(shop) == {"theme_name": "Dawn", "theme_changed": False, "blocks_lost": False}


def test_unknown_checks_are_not_remembered():
    shop = make_shop()
    ts.remember(shop, result("on", "Dawn"))
    ts.remember(shop, {"embed": "unknown", "theme_name": None, "blocks": None})
    assert shop.gift_settings["theme_check"]["embed"] == "on"
