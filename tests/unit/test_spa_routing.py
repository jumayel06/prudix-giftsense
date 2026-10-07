"""Dashboard entry points: served inside Shopify Admin, redirected otherwise.

A bare visit to giftsense.prudix.app has no Shopify context (every /api call
needs a session token), so it goes to the App Store listing instead of an
empty dashboard shell. Loads from Admin always carry shop / host / embedded /
id_token / hmac.
"""
import os

import pytest
from fastapi.testclient import TestClient

from app import main as app_main

_HAS_DIST = os.path.isdir(app_main._DIST)
needs_dist = pytest.mark.skipif(not _HAS_DIST, reason="dashboard/dist not built — SPA routes not mounted")

ADMIN_QS = "embedded=1&host=YWRtaW4&shop=demo.myshopify.com&id_token=t"


@pytest.fixture
def client():
    return TestClient(app_main.app)


@pytest.fixture
def listing_slug(monkeypatch):
    from core import config as core_config
    monkeypatch.setattr(core_config.settings, "app_store_listing_slug", "prudix-giftsense")


def test_shopify_context_detection():
    class R:
        def __init__(self, qs):
            self.query_params = dict(p.split("=") for p in qs.split("&") if p)
    assert app_main._has_shopify_context(R(ADMIN_QS))
    assert app_main._has_shopify_context(R("shop=demo.myshopify.com&hmac=x"))
    assert app_main._has_shopify_context(R("host=abc"))
    assert not app_main._has_shopify_context(R(""))
    assert not app_main._has_shopify_context(R("utm_source=google"))


@needs_dist
def test_bare_root_visit_redirects_to_app_store_listing(client, listing_slug):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://apps.shopify.com/prudix-giftsense"


@needs_dist
def test_redirect_falls_back_to_website_without_listing_slug(client, monkeypatch):
    from core import config as core_config
    monkeypatch.setattr(core_config.settings, "app_store_listing_slug", "")
    resp = client.get("/?utm_source=google", follow_redirects=False)
    assert resp.headers["location"] == "https://prudix.app"


@needs_dist
def test_admin_iframe_load_serves_the_dashboard(client, listing_slug):
    resp = client.get(f"/?{ADMIN_QS}", follow_redirects=False)
    assert resp.status_code == 200
    assert "<html" in resp.text.lower()


@needs_dist
def test_non_embedded_shop_visit_still_starts_oauth(client, listing_slug):
    resp = client.get("/?shop=demo.myshopify.com&hmac=x&timestamp=1", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"].startswith("/auth?")


@needs_dist
def test_dashboard_deep_links_follow_the_same_rule(client, listing_slug):
    bare = client.get("/settings", follow_redirects=False)
    assert bare.status_code == 302
    assert bare.headers["location"] == "https://apps.shopify.com/prudix-giftsense"
    inside = client.get(f"/settings?{ADMIN_QS}", follow_redirects=False)
    assert inside.status_code == 200


@needs_dist
def test_api_404s_stay_json(client, listing_slug):
    resp = client.get("/api/definitely-not-a-route", follow_redirects=False)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}


@needs_dist
def test_root_static_files_are_still_served(client, listing_slug):
    static = next((f for f in os.listdir(app_main._DIST)
                   if os.path.isfile(os.path.join(app_main._DIST, f)) and f != "index.html"), None)
    if static is None:
        pytest.skip("no root-level static file in dist")
    resp = client.get(f"/{static}", follow_redirects=False)
    assert resp.status_code == 200

