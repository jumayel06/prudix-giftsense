"""Is the GiftSense app embed switched on in the shop's live theme?

GraphQL Admin only (App Store rule 2.2.4; Commerce's equivalent used REST):
reads config/settings_data.json of the MAIN theme (read_themes scope) and
looks for `shopify://apps/<our handle>/blocks/app-embed/<uuid>`.

Lessons carried over from Commerce's product-block check:
  - Shopify may suffix app handles (-1, -2) in theme JSON → match
    `<handle base>(-<digits>)?`.
  - Slashes can arrive escaped (`shopify:\\/\\/apps`) → unescape first.
  - settings_data.json starts with a /* … */ comment → strip before parsing.
  - `current` can be a preset name instead of an object.
Any failure → "unknown" (the page then shows the setup steps, never an error).
"""
import json
import re

import structlog

from core.shopify_graphql import shopify_graphql_post

logger = structlog.get_logger()

EMBED_BLOCK = "app-embed"   # extensions/giftsense-theme/blocks/app-embed.liquid

QUERY = """
{
  currentAppInstallation { app { handle } }
  themes(first: 1, roles: [MAIN]) {
    nodes {
      name
      files(filenames: ["config/settings_data.json"], first: 1) {
        nodes { body { ... on OnlineStoreThemeFileBodyText { content } } }
      }
    }
  }
}
"""


def _current_blocks(data: dict) -> dict:
    current = data.get("current")
    if isinstance(current, str):   # a preset name
        current = (data.get("presets") or {}).get(current)
    return (current or {}).get("blocks") or {}


def embed_state(settings_text: str, app_handle: str | None) -> str:
    """"on" | "off" (added, switched off) | "missing" | "unknown"."""
    text = settings_text.replace("\\/", "/")
    text = re.sub(r"^\s*/\*.*?\*/", "", text, count=1, flags=re.S)
    try:
        blocks = _current_blocks(json.loads(text))
    except (ValueError, AttributeError):
        return "unknown"
    handle = rf"{re.escape(re.sub(r'-\d+$', '', app_handle))}(?:-\d+)?" if app_handle else r"[^/]+"
    pattern = re.compile(rf"^shopify://apps/{handle}/blocks/{EMBED_BLOCK}/")
    found = [b for b in blocks.values() if isinstance(b, dict) and pattern.match(str(b.get("type", "")))]
    if not found:
        return "missing"
    return "on" if any(not b.get("disabled") for b in found) else "off"


async def fetch_embed_status(shop_domain: str, token: str, gql=shopify_graphql_post) -> dict:
    try:
        resp = await gql(shop_domain, token, QUERY)
        if resp.status_code != 200:
            return {"embed": "unknown", "theme_name": None}
        data = resp.json().get("data") or {}
        handle = ((data.get("currentAppInstallation") or {}).get("app") or {}).get("handle")
        themes = (data.get("themes") or {}).get("nodes") or []
        if not themes:
            return {"embed": "unknown", "theme_name": None}
        files = (themes[0].get("files") or {}).get("nodes") or []
        content = ((files[0].get("body") or {}).get("content")) if files else None
        state = embed_state(content, handle) if content else "unknown"
        return {"embed": state, "theme_name": themes[0].get("name")}
    except Exception as e:  # noqa: BLE001 — setup hint only, never an error
        logger.warning("theme_status_failed", shop=shop_domain, error=str(e)[:200])
        return {"embed": "unknown", "theme_name": None}
