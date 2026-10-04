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


# ── Wrap cart guard (giftsense-cart.js), run in node against a fake cart ────

CART_HARNESS = r"""
const fs = require('fs');
const cart = JSON.parse(process.argv[1]);
const calls = [];
globalThis.window = globalThis;
globalThis.Shopify = { routes: { root: '/' } };
globalThis.location = { pathname: '/products/x', reload() { calls.push(['reload']); } };
globalThis.localStorage = { removeItem(k) { calls.push(['forget', k]); }, getItem() { return '1'; } };
globalThis.CustomEvent = class { constructor(n) { this.type = n; } };
globalThis.document = { dispatchEvent(e) { calls.push(['event', e.type]); } };
globalThis.XMLHttpRequest = function () {}; XMLHttpRequest.prototype.open = function () {};
globalThis.fetch = (url, opts) => {
  calls.push([url, opts && opts.body ? JSON.parse(opts.body) : null]);
  return Promise.resolve({ json: () => Promise.resolve(url.endsWith('cart.js') ? cart : {}) });
};
eval(fs.readFileSync(process.argv[2], 'utf8'));
setTimeout(() => console.log(JSON.stringify(calls)), 50);
"""


def run_guard(cart: dict) -> list:
    out = subprocess.run(["node", "-e", CART_HARNESS, json.dumps(cart), str(EXT / "assets" / "giftsense-cart.js")],
                         check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def line(key, **props):
    return {"key": key, "properties": props}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_cart_guard_removes_wrap_whose_gift_is_gone():
    groups = [{"id": "g1", "label": "Mom", "wrap": "Gold"}, {"id": "g2", "label": "Dad", "wrap": "Kraft"}]
    cart = {"attributes": {"_giftsense_gifts": json.dumps(groups)}, "items": [
        line("a", _giftsense_wrap_for="g1"),                  # Mom's gift was removed → orphan
        line("b", _giftsense_gift="g2"), line("c", _giftsense_wrap_for="g2"),
    ]}
    calls = run_guard(cart)
    changes = [c[1] for c in calls if c[0] == "/cart/change.js"]
    assert changes == [{"id": "a", "quantity": 0}]
    update = next(c[1] for c in calls if c[0] == "/cart/update.js")["attributes"]
    assert json.loads(update["_giftsense_gifts"]) == [{"id": "g1", "label": "Mom"},
                                                      {"id": "g2", "label": "Dad", "wrap": "Kraft"}]
    assert ["event", "cart:refresh"] in calls


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_cart_guard_clears_order_wrap_once_nothing_else_is_left():
    cart = {"attributes": {"Gift wrap": "Gold"}, "items": [line("w", _giftsense_wrap_for="order")]}
    calls = run_guard(cart)
    assert [c[1] for c in calls if c[0] == "/cart/change.js"] == [{"id": "w", "quantity": 0}]
    assert next(c[1] for c in calls if c[0] == "/cart/update.js") == {"attributes": {"Gift wrap": ""}}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_cart_guard_leaves_wrapped_gifts_alone_and_disarms_without_wrap():
    kept = run_guard({"attributes": {}, "items": [line("b", _giftsense_gift="order"),
                                                  line("w", _giftsense_wrap_for="order")]})
    assert [c for c in kept if c[0] != "/cart.js"] == []
    empty = run_guard({"attributes": {}, "items": [line("x")]})
    assert ["forget", "giftsense:wrap"] in empty


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_cart_guard_keeps_a_whole_order_wrap_while_items_remain():
    """Wrap added from the cart page or drawer covers the whole order, gift-marked or not."""
    calls = run_guard({"attributes": {"Gift wrap": "Gold"}, "items": [line("w", _giftsense_wrap_for="order"), line("x")]})
    assert [c for c in calls if c[0] in ("/cart/change.js", "/cart/update.js")] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_cart_guard_removes_a_greeting_card_whose_gift_is_gone():
    groups = [{"id": "g1", "label": "Mom", "card": "Birthday"}]
    calls = run_guard({"attributes": {"_giftsense_gifts": json.dumps(groups)},
                       "items": [line("c", _giftsense_card_for="g1"), line("x")]})
    assert [c[1] for c in calls if c[0] == "/cart/change.js"] == [{"id": "c", "quantity": 0}]
    update = next(c[1] for c in calls if c[0] == "/cart/update.js")["attributes"]
    assert json.loads(update["_giftsense_gifts"]) == [{"id": "g1", "label": "Mom"}]
