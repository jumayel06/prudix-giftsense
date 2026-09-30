"""Scopes live in 3 places (CLAUDE.md): both tomls and core/shopify_auth.SCOPES.
They must match exactly, and stay Level-1 (no customer scopes)."""
import tomllib
from pathlib import Path

from core.shopify_auth import SCOPES

ROOT = Path(__file__).resolve().parents[2]


def toml_scopes(name):
    return set(tomllib.loads((ROOT / name).read_text())["access_scopes"]["scopes"].split(","))


def test_scopes_match_everywhere():
    code = set(SCOPES.split(","))
    assert toml_scopes("shopify.app.dev.toml") == code == toml_scopes("shopify.app.prod.toml")


def test_wrap_product_can_be_published():
    # The hidden gift-wrap product must be published to the Online Store to be addable to carts.
    assert "write_publications" in SCOPES.split(",")


def test_no_customer_scopes():
    assert not any("customer" in s for s in SCOPES.split(","))
