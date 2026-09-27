"""Tests for the Sentry `before_send` scrubber in app/main.py.

We use Sentry's `send_default_pii=True` for richer error context, which means
request headers and query strings reach Sentry. Shopify requests carry the
`X-Shopify-Access-Token` header and OAuth callbacks carry `code=` in the
query string — both are credentials. The scrubber must redact them before
events leave the SDK.
"""

from app.main import _scrub_shopify_secrets


class TestSentryScrubber:

    def test_scrubs_shopify_access_token_header(self):
        event = {"request": {"headers": {"X-Shopify-Access-Token": "shpat_secret123"}}}
        scrubbed = _scrub_shopify_secrets(event, hint=None)
        assert scrubbed["request"]["headers"]["X-Shopify-Access-Token"] == "[Filtered]"

    def test_scrubs_authorization_header(self):
        event = {"request": {"headers": {"Authorization": "Bearer abc.def.ghi"}}}
        scrubbed = _scrub_shopify_secrets(event, hint=None)
        assert scrubbed["request"]["headers"]["Authorization"] == "[Filtered]"

    def test_scrubs_cookie_header(self):
        event = {"request": {"headers": {"Cookie": "session=abc"}}}
        scrubbed = _scrub_shopify_secrets(event, hint=None)
        assert scrubbed["request"]["headers"]["Cookie"] == "[Filtered]"

    def test_scrubs_hmac_header(self):
        """Shopify webhook verification header — also sensitive."""
        event = {"request": {"headers": {"X-Shopify-Hmac-Sha256": "hmacvalue"}}}
        scrubbed = _scrub_shopify_secrets(event, hint=None)
        assert scrubbed["request"]["headers"]["X-Shopify-Hmac-Sha256"] == "[Filtered]"

    def test_case_insensitive_header_match(self):
        """Header case varies in practice — lowercase, titlecase, ALLCAPS."""
        event = {"request": {"headers": {"x-shopify-access-token": "shpat_secret"}}}
        scrubbed = _scrub_shopify_secrets(event, hint=None)
        assert scrubbed["request"]["headers"]["x-shopify-access-token"] == "[Filtered]"

    def test_preserves_non_sensitive_headers(self):
        event = {
            "request": {
                "headers": {
                    "X-Shopify-Access-Token": "secret",
                    "User-Agent": "Mozilla/5.0",
                    "Content-Type": "application/json",
                }
            }
        }
        scrubbed = _scrub_shopify_secrets(event, hint=None)
        assert scrubbed["request"]["headers"]["User-Agent"] == "Mozilla/5.0"
        assert scrubbed["request"]["headers"]["Content-Type"] == "application/json"

    def test_scrubs_oauth_code_in_query_string(self):
        event = {"request": {"query_string": "shop=foo.myshopify.com&code=abc123&hmac=def&state=xyz"}}
        scrubbed = _scrub_shopify_secrets(event, hint=None)
        qs = scrubbed["request"]["query_string"]
        assert "code=[Filtered]" in qs
        assert "state=[Filtered]" in qs
        assert "hmac=[Filtered]" in qs
        assert "shop=foo.myshopify.com" in qs  # non-sensitive preserved

    def test_no_request_field_does_not_crash(self):
        scrubbed = _scrub_shopify_secrets({}, hint=None)
        assert scrubbed == {}

    def test_no_headers_does_not_crash(self):
        scrubbed = _scrub_shopify_secrets({"request": {}}, hint=None)
        assert scrubbed == {"request": {}}

    def test_headers_not_dict_does_not_crash(self):
        """Sentry may pass headers as a list of tuples in some integrations."""
        event = {"request": {"headers": [("X-Shopify-Access-Token", "secret")]}}
        scrubbed = _scrub_shopify_secrets(event, hint=None)
        # We don't scrub list-form (defensively bail), just don't crash.
        assert scrubbed == event


class TestHTTPExceptionFiltering:
    """HTTPException-derived events are intentional client-facing errors —
    Sentry should drop them so the issue feed stays focused on real bugs."""

    def test_fastapi_http_exception_dropped(self):
        """502 raised by a route handler on Judge.me timeout, OpenAI timeout,
        Shopify failure, etc. — none of these are bugs."""
        from fastapi.exceptions import HTTPException

        event = {"request": {"headers": {}}}
        hint = {"exc_info": (HTTPException, HTTPException(status_code=502, detail="Could not reach Judge.me"), None)}
        assert _scrub_shopify_secrets(event, hint) is None

    def test_starlette_http_exception_dropped(self):
        """Starlette's HTTPException (parent class) also dropped — covers any
        path that raises the base class directly."""
        from starlette.exceptions import HTTPException as StarletteHTTPException

        event = {"request": {"headers": {}}}
        hint = {"exc_info": (StarletteHTTPException, StarletteHTTPException(status_code=404, detail="Not found"), None)}
        assert _scrub_shopify_secrets(event, hint) is None

    def test_real_exception_not_dropped(self):
        """AttributeError, KeyError, etc. are real bugs — must still be sent
        to Sentry (just with secrets scrubbed)."""
        event = {"request": {"headers": {"X-Shopify-Access-Token": "secret"}}}
        hint = {"exc_info": (AttributeError, AttributeError("'NoneType' object has no attribute 'foo'"), None)}
        scrubbed = _scrub_shopify_secrets(event, hint)
        assert scrubbed is not None
        assert scrubbed["request"]["headers"]["X-Shopify-Access-Token"] == "[Filtered]"

    def test_missing_exc_info_does_not_crash(self):
        """Some Sentry events (manual capture_message, breadcrumbs) have no
        exc_info — should be processed normally, not dropped."""
        event = {"request": {"headers": {}}}
        assert _scrub_shopify_secrets(event, hint={}) is not None
        assert _scrub_shopify_secrets(event, hint=None) is not None
