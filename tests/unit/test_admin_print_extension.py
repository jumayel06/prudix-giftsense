"""Admin print action extension (extensions/giftsense-print) guards."""
import tomllib
from pathlib import Path

from app.main import app

EXT = Path(__file__).resolve().parents[2] / "extensions" / "giftsense-print"


def test_targets_the_order_print_menu():
    # Shopify allows exactly one print target per extension (deploy rejects more).
    cfg = tomllib.loads((EXT / "shopify.extension.toml").read_text())
    exts = cfg["extensions"]
    assert all(e["type"] == "ui_extension" and len(e["targeting"]) == 1 for e in exts)
    assert len({e["handle"] for e in exts}) == len(exts)
    assert [e["targeting"][0]["target"] for e in exts] == [
        "admin.order-details.print-action.render", "admin.order-index.selection-print-action.render"]
    assert all((EXT / e["targeting"][0]["module"]).exists() for e in exts)


def test_src_is_a_relative_path_to_a_real_backend_route():
    # Shopify loads relative src paths from the app URL with a session token.
    source = (EXT / "src" / "PrintAction.jsx").read_text()
    assert "`/print/gifts?" in source and "https://" not in source
    assert "/print/gifts" in {r.path for r in app.routes}
    assert ".slice(0, 50)" in source      # matches the backend's MAX_ORDERS
