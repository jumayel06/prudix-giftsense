"""Content-Security-Policy `frame-ancestors` on HTML responses.

Shopify requires embedded apps to say who may frame them: the merchant's own
shop domain + Shopify Admin (clickjacking protection, checked by Shopify).
Only `frame-ancestors` is set — no script/style directives — so App Bridge,
Polaris and our bundle are unaffected. JSON, redirects and static assets are
left alone; the internal admin tool (ADMIN_PATH) can't be framed at all.

Pure ASGI (not BaseHTTPMiddleware) so streaming responses and background
tasks behave exactly as before.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs

_SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")
_FALLBACK = "frame-ancestors https://admin.shopify.com https://*.myshopify.com;"
_ADMIN_TOOL = "frame-ancestors 'none';"


def frame_ancestors_policy(path: str, query_string: bytes) -> str:
    from app.admin.auth import is_admin_tool_path
    if is_admin_tool_path(path) or path == "/admin" or path.startswith("/admin/"):
        return _ADMIN_TOOL
    shop = (parse_qs(query_string.decode("latin-1")).get("shop") or [""])[0].strip().lower()
    if _SHOP_RE.match(shop):
        return f"frame-ancestors https://{shop} https://admin.shopify.com;"
    return _FALLBACK


class FrameAncestorsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        policy = frame_ancestors_policy(scope.get("path", ""), scope.get("query_string", b""))

        async def send_with_csp(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                content_type = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                has_csp = any(k.lower() == b"content-security-policy" for k, _ in headers)
                if content_type.lower().startswith(b"text/html") and not has_csp:
                    headers.append((b"content-security-policy", policy.encode("latin-1")))
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_csp)
