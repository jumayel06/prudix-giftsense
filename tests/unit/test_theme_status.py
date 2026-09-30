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
    assert result == {"embed": "on", "theme_name": "Dawn"}
    assert "roles: [MAIN]" in gql.await_args.args[2]


@pytest.mark.asyncio
async def test_fetch_failure_is_unknown_not_an_error():
    gql = AsyncMock(return_value=MagicMock(status_code=500))
    assert (await ts.fetch_embed_status("shop.myshopify.com", "tok", gql=gql))["embed"] == "unknown"
