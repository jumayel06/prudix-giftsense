"""frame-ancestors CSP: Shopify-required on embedded HTML, nothing else touched."""
import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.testclient import TestClient

from app.csp import FrameAncestorsMiddleware, frame_ancestors_policy
from tests.unit.test_spa_routing import ADMIN_QS, needs_dist

CSP = "content-security-policy"


@pytest.mark.parametrize("path,qs,expected", [
    ("/", b"shop=demo.myshopify.com&host=x",
     "frame-ancestors https://demo.myshopify.com https://admin.shopify.com;"),
    ("/settings", b"shop=Demo-Store.MyShopify.com",
     "frame-ancestors https://demo-store.myshopify.com https://admin.shopify.com;"),
    ("/", b"", "frame-ancestors https://admin.shopify.com https://*.myshopify.com;"),
    # Anything that isn't a plain *.myshopify.com domain falls back — never echoed.
    ("/", b"shop=evil.com", "frame-ancestors https://admin.shopify.com https://*.myshopify.com;"),
    ("/", b"shop=x.myshopify.com%20https://evil.com",
     "frame-ancestors https://admin.shopify.com https://*.myshopify.com;"),
    ("/", b"shop=x.myshopify.com;script-src%20*",
     "frame-ancestors https://admin.shopify.com https://*.myshopify.com;"),
    ("/admin", b"shop=demo.myshopify.com", "frame-ancestors 'none';"),
    ("/admin/shops", b"", "frame-ancestors 'none';"),
    ("/administrator", b"", "frame-ancestors https://admin.shopify.com https://*.myshopify.com;"),
])
def test_policy(path, qs, expected):
    assert frame_ancestors_policy(path, qs) == expected


def _mini_app():
    app = FastAPI()
    app.add_middleware(FrameAncestorsMiddleware)

    @app.get("/page")
    def page():
        return HTMLResponse("<html></html>")

    @app.get("/data")
    def data():
        return JSONResponse({"ok": 1})

    @app.get("/go")
    def go():
        return RedirectResponse("/page")

    @app.get("/own")
    def own():
        return HTMLResponse("<html></html>", headers={"Content-Security-Policy": "frame-ancestors 'self';"})
    return TestClient(app)


def test_only_html_responses_get_the_header():
    c = _mini_app()
    assert c.get("/page?shop=a.myshopify.com").headers[CSP] == \
        "frame-ancestors https://a.myshopify.com https://admin.shopify.com;"
    assert CSP not in c.get("/data?shop=a.myshopify.com").headers
    assert CSP not in c.get("/go", follow_redirects=False).headers


def test_existing_policy_is_not_overridden():
    assert _mini_app().get("/own").headers[CSP] == "frame-ancestors 'self';"


def test_no_x_frame_options_added():
    assert "x-frame-options" not in _mini_app().get("/page").headers


@needs_dist
def test_real_app_dashboard_shell_carries_the_header():
    from app.main import app
    resp = TestClient(app).get(f"/?{ADMIN_QS}", follow_redirects=False)
    assert resp.status_code == 200
    assert resp.headers[CSP] == "frame-ancestors https://demo.myshopify.com https://admin.shopify.com;"


def test_real_app_api_json_is_untouched():
    from app.main import app
    resp = TestClient(app).get("/health")
    assert CSP not in resp.headers
