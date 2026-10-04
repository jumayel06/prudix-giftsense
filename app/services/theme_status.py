"""Is the GiftSense app embed switched on in the shop's live theme, and where
are its blocks (Find a gift section, Gift options) placed?

GraphQL Admin only (App Store rule 2.2.4; Commerce's equivalent used REST):
reads config/settings_data.json of the MAIN theme (read_themes scope) and
looks for `shopify://apps/<our handle>/blocks/app-embed/<uuid>`.

Lessons carried over from Commerce's product-block check:
  - Shopify may suffix app handles (-1, -2) in theme JSON → match
    `<handle base>(-<digits>)?`.
  - Slashes can arrive escaped (`shopify:\\/\\/apps`) → unescape first.
  - settings_data.json starts with a /* … */ comment → strip before parsing.
  - `current` can be a preset name instead of an object.
Blocks: every JSON template (templates/*.json) and section group
(sections/*.json) is scanned for `.../blocks/gift-finder/` and
`.../blocks/gift-options/`, skipping disabled sections and blocks.
Any failure → "unknown" (the page then shows the setup steps, never an error).
"""
import json
import re
from datetime import datetime, timezone

import structlog

from core.shopify_graphql import shopify_graphql_post

logger = structlog.get_logger()

EMBED_BLOCK = "app-embed"   # extensions/giftsense-theme/blocks/app-embed.liquid
PLACED_BLOCKS = ("gift-finder", "gift-options")

QUERY = """
{
  currentAppInstallation { app { handle } }
  themes(first: 1, roles: [MAIN]) {
    nodes {
      name
      files(filenames: ["config/settings_data.json"], first: 1) {
        nodes { body { ... on OnlineStoreThemeFileBodyText { content } } }
      }
      templates: files(filenames: ["templates/*.json", "sections/*.json"], first: 250) {
        nodes { filename body { ... on OnlineStoreThemeFileBodyText { content } } }
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


def _load_theme_json(text: str):
    text = text.replace("\\/", "/")
    text = re.sub(r"^\s*/\*.*?\*/", "", text, count=1, flags=re.S)
    return json.loads(text)


def _block_pattern(app_handle: str | None, block: str) -> re.Pattern:
    handle = rf"{re.escape(re.sub(r'-\d+$', '', app_handle))}(?:-\d+)?" if app_handle else r"[^/]+"
    return re.compile(rf"^shopify://apps/{handle}/blocks/{block}/")


def embed_state(settings_text: str, app_handle: str | None) -> str:
    """"on" | "off" (added, switched off) | "missing" | "unknown"."""
    try:
        blocks = _current_blocks(_load_theme_json(settings_text))
    except (ValueError, AttributeError):
        return "unknown"
    pattern = _block_pattern(app_handle, EMBED_BLOCK)
    found = [b for b in blocks.values() if isinstance(b, dict) and pattern.match(str(b.get("type", "")))]
    if not found:
        return "missing"
    return "on" if any(not b.get("disabled") for b in found) else "off"


_PAGE_NAMES = {"index": "Home page", "product": "Product pages", "collection": "Collection pages",
               "cart": "Cart page", "page": "Pages", "search": "Search page", "list-collections": "Collections list",
               "blog": "Blog", "article": "Blog posts", "404": "404 page"}


def page_label(filename: str) -> str:
    """templates/product.gift.json → "Product pages (gift)"; sections/header-group.json → "Header"."""
    name = filename.rsplit("/", 1)[-1].removesuffix(".json")
    if filename.startswith("sections/"):
        return name.removesuffix("-group").replace("-", " ").capitalize()
    base, _, alt = name.partition(".")
    label = _PAGE_NAMES.get(base, base.replace("-", " ").capitalize())
    return f"{label} ({alt})" if alt else label


def _has_block(node, pattern: re.Pattern) -> bool:
    """An enabled section/block of this type anywhere under node."""
    if isinstance(node, dict):
        if node.get("disabled"):
            return False
        if isinstance(node.get("type"), str) and pattern.match(node["type"]):
            return True
        return any(_has_block(v, pattern) for k, v in node.items() if k in ("sections", "blocks") or isinstance(v, dict))
    if isinstance(node, list):
        return any(_has_block(v, pattern) for v in node)
    return False


def block_placements(files: list[dict], app_handle: str | None) -> dict[str, list[str]]:
    """{"gift-finder": ["Home page"], "gift-options": ["Product pages", "Cart page"]}"""
    out: dict[str, list[str]] = {b: [] for b in PLACED_BLOCKS}
    patterns = {b: _block_pattern(app_handle, b) for b in PLACED_BLOCKS}
    for f in files:
        content = ((f.get("body") or {}).get("content")) or ""
        if "/blocks/gift-" not in content.replace("\\/", "/"):
            continue
        try:
            data = _load_theme_json(content)
        except ValueError:
            continue
        for block, pattern in patterns.items():
            label = page_label(f.get("filename") or "")
            if _has_block(data, pattern) and label not in out[block]:
                out[block].append(label)
    return out


async def fetch_embed_status(shop_domain: str, token: str, gql=shopify_graphql_post) -> dict:
    try:
        resp = await gql(shop_domain, token, QUERY)
        if resp.status_code != 200:
            return {"embed": "unknown", "theme_name": None, "blocks": None}
        data = resp.json().get("data") or {}
        handle = ((data.get("currentAppInstallation") or {}).get("app") or {}).get("handle")
        themes = (data.get("themes") or {}).get("nodes") or []
        if not themes:
            return {"embed": "unknown", "theme_name": None, "blocks": None}
        files = (themes[0].get("files") or {}).get("nodes") or []
        content = ((files[0].get("body") or {}).get("content")) if files else None
        state = embed_state(content, handle) if content else "unknown"
        placed = block_placements((themes[0].get("templates") or {}).get("nodes") or [], handle)
        return {"embed": state, "theme_name": themes[0].get("name"), "blocks": placed}
    except Exception as e:  # noqa: BLE001 — setup hint only, never an error
        logger.warning("theme_status_failed", shop=shop_domain, error=str(e)[:200])
        return {"embed": "unknown", "theme_name": None, "blocks": None}


def remember(shop, result: dict) -> None:
    """Store the latest live-theme check on the shop (gift_settings["theme_check"])
    for the dashboard-wide warning. `theme_changed` / `blocks_lost`: the live
    theme differs from the last check, and sections placed on the old one are
    missing from it (app embeds and blocks are saved per theme)."""
    if result.get("embed") == "unknown":
        return
    prev = (shop.gift_settings or {}).get("theme_check") or {}
    same_theme = prev.get("theme_name") == result.get("theme_name")
    switched = bool(prev.get("theme_name")) and not same_theme      # a different theme went live
    had = any((prev.get("blocks") or {}).values())
    has = any((result.get("blocks") or {}).values())
    # Both flags stick across re-checks of the same theme until it's fixed.
    theme_changed = switched or (same_theme and prev.get("theme_changed", False) and result["embed"] != "on")
    blocks_lost = (switched and had and not has) or (same_theme and prev.get("blocks_lost", False) and not has)
    check = {"embed": result["embed"], "theme_name": result.get("theme_name"), "blocks": result.get("blocks") or {},
             "theme_changed": theme_changed, "blocks_lost": blocks_lost,
             "checked_at": datetime.now(timezone.utc).isoformat(),
             # Theme we already emailed the owner about (app/workers/theme.py).
             "emailed_for": prev.get("emailed_for")}
    shop.gift_settings = {**(shop.gift_settings or {}), "theme_check": check}


def warning(shop) -> dict | None:
    """What the dashboard banner shows, or None when GiftSense is on in the live theme."""
    check = (shop.gift_settings or {}).get("theme_check") or {}
    if check.get("embed") not in ("off", "missing"):
        return None
    return {"theme_name": check.get("theme_name"), "theme_changed": bool(check.get("theme_changed")),
            "blocks_lost": bool(check.get("blocks_lost"))}
