"""Admin sign-in: password + TOTP, signed session cookie, attempt limit, hidden path."""
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin import auth
from app.admin.login import router as login_router
from app.admin.router import router as admin_router
from app.main import app

SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238 test key "12345678901234567890"


@pytest.fixture(autouse=True)
def admin_env(monkeypatch):
    from core import config as core_config
    s = core_config.settings
    monkeypatch.setattr(s, "internal_admin_username", "ops")
    monkeypatch.setattr(s, "internal_admin_password", "correct horse battery")
    monkeypatch.setattr(s, "admin_totp_secret", SECRET)
    monkeypatch.setattr(s, "redis_url", "")
    fresh = auth._MemoryStore()
    monkeypatch.setattr(auth, "_memory", fresh)
    monkeypatch.setattr(auth, "_store", fresh)


def _code(offset_steps: int = 0) -> str:
    return auth.totp_at(SECRET, int(time.time() // auth.TOTP_STEP) + offset_steps)


def _login(client, password="correct horse battery", code=None, next_="", ip="203.0.113.7", username="ops"):
    return client.post(
        "/admin/login",
        data={"username": username, "password": password, "code": _code() if code is None else code, "next": next_},
        headers={"x-forwarded-for": ip},
        follow_redirects=False,
    )


# ── TOTP ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("t,expected", [(59, "287082"), (1111111109, "081804"), (1234567890, "005924")])
def test_totp_matches_rfc6238_vectors(t, expected):
    assert auth.totp_at(SECRET, t // 30) == expected


def test_totp_accepts_one_step_of_clock_drift_only():
    now = 1234567890
    for step, ok in [(-2, False), (-1, True), (0, True), (1, True), (2, False)]:
        code = auth.totp_at(SECRET, now // 30 + step)
        assert (auth.totp_match(SECRET, code, now=now) is not None) is ok, step


def test_totp_rejects_malformed_codes():
    for bad in ["", "12345", "1234567", "abcdef", None]:
        assert auth.totp_match(SECRET, bad) is None
    assert auth.totp_match(SECRET, _code()[:3] + " " + _code()[3:]) is not None  # "123 456" is fine


# ── Session token ────────────────────────────────────────────────────────────

def test_session_roundtrip_tamper_and_expiry():
    token = auth.make_session("ops", now=1000)
    assert auth.read_session(token, now=1001) == "ops"
    assert auth.read_session(token, now=1000 + auth.SESSION_SECONDS) is None
    body, sig = token.split(".")
    assert auth.read_session(body + "." + sig[:-2] + "AA", now=1001) is None
    assert auth.read_session("garbage", now=1001) is None
    assert auth.read_session(None) is None


def test_changing_password_or_totp_secret_signs_everyone_out(monkeypatch):
    from core import config as core_config
    token = auth.make_session("ops")
    monkeypatch.setattr(core_config.settings, "internal_admin_password", "new password")
    assert auth.read_session(token) is None
    monkeypatch.setattr(core_config.settings, "internal_admin_password", "correct horse battery")
    assert auth.read_session(token) == "ops"
    monkeypatch.setattr(core_config.settings, "admin_totp_secret", "JBSWY3DPEHPK3PXP")
    assert auth.read_session(token) is None


@pytest.mark.parametrize("target,expected", [
    ("/admin/shops?q=x", "/admin/shops?q=x"),
    ("/docs", "/docs"),
    ("https://evil.com", "/admin/"),
    ("//evil.com/admin/", "/admin/"),
    ("/\\evil.com", "/admin/"),
    ("/api/stats", "/admin/"),
    ("/admin/login", "/admin/"),
    ("", "/admin/"),
])
def test_safe_next_only_allows_our_admin_pages(target, expected):
    assert auth.safe_next(target) == expected


def test_client_ip_uses_rightmost_forwarded_entry():
    class R:
        headers = {"x-forwarded-for": "1.1.1.1, 9.9.9.9"}
        client = None
    assert auth.client_ip(R) == "9.9.9.9"


# ── HTTP flow ────────────────────────────────────────────────────────────────

def test_admin_page_redirects_to_login_when_signed_out():
    resp = TestClient(app).get("/admin/shops?q=a", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login?next=%2Fadmin%2Fshops%3Fq%3Da"


def test_admin_post_without_session_is_401():
    resp = TestClient(app).post("/admin/support/x/update", data={"status": "open"}, follow_redirects=False)
    assert resp.status_code == 401


def test_login_page_renders_mobile_friendly_form():
    resp = TestClient(app).get("/admin/login")
    assert resp.status_code == 200
    html = resp.text
    assert 'autocapitalize="none"' in html and 'autocomplete="one-time-code"' in html
    assert 'action="/admin/login"' in html
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["content-security-policy"] == "frame-ancestors 'none';"


def test_code_field_hidden_when_totp_not_configured(monkeypatch):
    from core import config as core_config
    monkeypatch.setattr(core_config.settings, "admin_totp_secret", "")
    assert 'name="code"' not in TestClient(app).get("/admin/login").text
    resp = _login(TestClient(app), code="")
    assert resp.status_code == 303


def test_successful_login_sets_secure_session_and_grants_access():
    client = TestClient(app)
    resp = _login(client, next_="/docs")
    assert resp.status_code == 303 and resp.headers["location"] == "/docs"
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith(f"{auth.COOKIE_NAME}=")
    assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower() and "Max-Age=43200" in cookie
    assert client.get("/docs", follow_redirects=False).status_code == 200


def test_session_cookie_is_secure_outside_dev(monkeypatch):
    from core import config as core_config
    monkeypatch.setattr(core_config.settings, "app_env", "production")
    assert auth.cookie_secure() is True
    monkeypatch.setattr(core_config.settings, "app_env", "test")
    assert auth.cookie_secure() is False


@pytest.mark.parametrize("kwargs", [
    {"password": "wrong"},
    {"username": "Ops"},          # case matters (the form turns auto-capitalise off)
    {"code": "000000"},
    {"code": ""},
])
def test_bad_credentials_are_rejected_with_a_generic_message(kwargs):
    resp = _login(TestClient(app), **kwargs)
    assert resp.status_code == 401
    assert "didn&#39;t match" in resp.text or "didn't match" in resp.text
    assert "set-cookie" not in resp.headers


def test_totp_code_cannot_be_reused():
    code = _code()
    assert _login(TestClient(app), code=code).status_code == 303
    assert _login(TestClient(app), code=code).status_code == 401


def test_lockout_after_five_failures_per_ip():
    client = TestClient(app)
    for _ in range(auth.MAX_FAILURES):
        assert _login(client, password="wrong").status_code == 401
    locked = _login(client)                           # even the right details
    assert locked.status_code == 429 and "Too many attempts" in locked.text
    assert _login(client, ip="198.51.100.2").status_code == 303   # other IPs unaffected


def test_success_resets_the_failure_count():
    client = TestClient(app)
    for _ in range(auth.MAX_FAILURES - 1):
        _login(client, password="wrong")
    assert _login(client).status_code == 303
    for _ in range(auth.MAX_FAILURES - 1):
        assert _login(client, password="wrong").status_code == 401


def test_open_redirect_is_blocked_after_login():
    resp = _login(TestClient(app), next_="https://evil.com/")
    assert resp.headers["location"] == "/admin/"


def test_logout_clears_the_session():
    client = TestClient(app)
    _login(client)
    resp = client.post("/admin/logout", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/admin/login"
    assert client.get("/docs", follow_redirects=False).status_code == 303


def test_signed_in_visit_to_login_goes_straight_through():
    client = TestClient(app)
    _login(client)
    resp = client.get("/admin/login?next=/docs", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/docs"


def test_unconfigured_admin_is_503(monkeypatch):
    from core import config as core_config
    monkeypatch.setattr(core_config.settings, "internal_admin_password", "")
    assert TestClient(app).get("/admin/login").status_code == 503
    assert TestClient(app).get("/admin/", follow_redirects=False).status_code == 503


def test_store_outage_falls_back_instead_of_locking_out(monkeypatch):
    class BrokenStore:
        async def incr(self, *a): raise ConnectionError("down")
        async def get_int(self, *a): raise ConnectionError("down")
        async def set_int(self, *a): raise ConnectionError("down")
        async def delete(self, *a): raise ConnectionError("down")
    monkeypatch.setattr(auth, "_store", BrokenStore())
    assert _login(TestClient(app)).status_code == 303


# ── Hidden admin path ────────────────────────────────────────────────────────

def test_custom_admin_path_moves_everything(monkeypatch):
    from core import config as core_config
    monkeypatch.setattr(core_config.settings, "admin_path", "/ops-7f3k9q/")  # trailing slash tolerated
    assert auth.admin_path() == "/ops-7f3k9q"

    hidden = FastAPI()
    hidden.include_router(login_router, prefix=auth.admin_path())
    hidden.include_router(admin_router, prefix=auth.admin_path())
    client = TestClient(hidden)

    assert client.get("/admin/", follow_redirects=False).status_code == 404
    resp = client.get("/ops-7f3k9q/shops", follow_redirects=False)
    assert resp.headers["location"].startswith("/ops-7f3k9q/login?next=%2Fops-7f3k9q%2Fshops")
    assert 'action="/ops-7f3k9q/login"' in client.get("/ops-7f3k9q/login").text
    assert auth.safe_next("/admin/shops") == "/ops-7f3k9q/"

    from app.csp import frame_ancestors_policy
    assert frame_ancestors_policy("/ops-7f3k9q/login", b"") == "frame-ancestors 'none';"


@pytest.mark.parametrize("bad", ["ops", "/", "/ops path", "/../etc", "/ops?x=1", ""])
def test_invalid_admin_path_falls_back_to_default(monkeypatch, bad):
    from core import config as core_config
    monkeypatch.setattr(core_config.settings, "admin_path", bad)
    assert auth.admin_path() == "/admin"
