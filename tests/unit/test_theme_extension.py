"""Theme app extension guards (extensions/giftsense-theme).

The storefront JS can't run under pytest, but these catch the mistakes that
would ship silently: a bootstrap that grows past the page-weight budget, a
broken block schema, an asset a block points at that doesn't exist, markup
injected with innerHTML, or an API version drifting from the app's."""
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parents[2] / "extensions" / "giftsense-theme"
ROOT = EXT.parents[1]
BOOTSTRAP_MAX_BYTES = 10 * 1024


def schema(block: str) -> dict:
    text = (EXT / "blocks" / block).read_text()
    return json.loads(re.search(r"{% schema %}(.*?){% endschema %}", text, re.S).group(1))


def test_bootstrap_stays_under_10kb():
    # Loaded on every storefront page; the finder UI loads on first click only.
    assert (EXT / "assets" / "giftsense.js").stat().st_size < BOOTSTRAP_MAX_BYTES


BLOCKS = sorted(p.name for p in (EXT / "blocks").glob("*.liquid"))


def test_block_schemas_are_valid_with_the_right_targets():
    assert schema("app-embed.liquid")["target"] == "body"
    assert schema("gift-finder.liquid")["target"] == "section"
    gift_options = schema("gift-options.liquid")
    assert gift_options["target"] == "section"
    assert gift_options["enabled_on"]["templates"] == ["product", "cart"]


def test_every_referenced_asset_exists():
    for block in BLOCKS:
        for asset in re.findall(r"'([\w.-]+)' \| asset_url", (EXT / "blocks" / block).read_text()):
            assert (EXT / "assets" / asset).exists(), f"{block} references missing {asset}"


def test_no_html_injection_in_storefront_js():
    for js in (EXT / "assets").glob("*.js"):
        text = js.read_text()
        assert "innerHTML" not in text and "insertAdjacentHTML" not in text and "document.write" not in text, js.name


def test_all_calls_go_through_the_app_proxy():
    for block in BLOCKS:
        assert 'data-api="/apps/giftsense"' in (EXT / "blocks" / block).read_text()


def test_api_version_matches_the_app():
    ext = tomllib.loads((EXT / "shopify.extension.toml").read_text())
    for cfg in ("shopify.app.dev.toml", "shopify.app.prod.toml"):
        app = tomllib.loads((ROOT / cfg).read_text())
        assert ext["api_version"] == app["webhooks"]["api_version"], cfg


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_parses():
    for js in (EXT / "assets").glob("*.js"):
        subprocess.run(["node", "--check", str(js)], check=True)
