"""Admin print action extension (extensions/giftsense-print) guards."""
import tomllib
from pathlib import Path

from app.main import app

EXT = Path(__file__).resolve().parents[2] / "extensions" / "giftsense-print"


def test_targets_the_order_print_menu():
    cfg = tomllib.loads((EXT / "shopify.extension.toml").read_text())
    [ext] = cfg["extensions"]
    assert ext["type"] == "ui_extension"
    assert [t["target"] for t in ext["targeting"]] == [
        "admin.order-details.print-action.render", "admin.order-index.selection-print-action.render"]
    assert all((EXT / t["module"]).exists() for t in ext["targeting"])


def test_src_is_a_relative_path_to_a_real_backend_route():
    # Shopify loads relative src paths from the app URL with a session token.
    source = (EXT / "src" / "PrintAction.jsx").read_text()
    assert "`/print/gifts?" in source and "https://" not in source
    assert "/print/gifts" in {r.path for r in app.routes}
    assert ".slice(0, 50)" in source      # matches the backend's MAX_ORDERS
