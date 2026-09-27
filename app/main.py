import asyncio
import logging
import os
import sys
import sentry_sdk
import structlog
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.routes import auth, billing, webhooks
from app.routes import settings as settings_routes
from app.admin.auth import require_admin
from core.config import settings

# ── Logging configuration ────────────────────────────────────────────────────
# Production (Railway): JSON to stdout. Railway's log aggregator parses JSON
# fields automatically, so we get structured search ("filter to shop_id=X",
# "show errors from /api/faq/*").
# Dev: colored console output. Easier to scan during local debugging.
_is_prod = settings.is_production
_log_level = logging.DEBUG if not _is_prod else logging.INFO

logging.basicConfig(
    format="%(message)s",
    level=_log_level,
    handlers=[logging.StreamHandler()],
)

# structlog uses PrintLoggerFactory (below), which writes straight to stdout and
# never touches stdlib logging — so Sentry's LoggingIntegration can't see these
# events. This processor bridges the gap: WARNING/ERROR/CRITICAL log calls are
# forwarded to Sentry as messages so graceful-degradation paths (e.g. a Shopify
# API poll that logs a warning and moves on) surface in the Sentry UI next to
# unhandled exceptions. No-op unless SENTRY_DSN is set. Grouped by the log event
# name, so a repeating warning is one Sentry issue with a count, not a flood.
def _forward_to_sentry(logger, method_name, event_dict):
    if not settings.sentry_dsn:
        return event_dict
    level = event_dict.get("level")
    if level not in ("warning", "error", "critical"):
        return event_dict
    try:
        with sentry_sdk.new_scope() as scope:
            for key, value in event_dict.items():
                if key not in ("event", "level", "timestamp"):
                    scope.set_extra(key, value)
            scope.set_tag("log_event", str(event_dict.get("event", "")))
            sentry_sdk.capture_message(
                str(event_dict.get("event", "log")),
                level="warning" if level == "warning" else "error",
            )
    except Exception:  # noqa: BLE001 — observability must never break logging
        pass
    return event_dict


structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _forward_to_sentry,
        # JSON in prod (machine-parseable), pretty console in dev (human-readable)
        structlog.processors.JSONRenderer() if _is_prod else structlog.dev.ConsoleRenderer(colors=True),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(_log_level),
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()

# Sentry — initialize before FastAPI so the SDK's middleware can wrap requests.
# Skipped when SENTRY_DSN is empty so dev/staging don't ship noise to a project
# that isn't theirs. Sample 100% of errors but only 10% of perf traces (we don't
# need full APM, just exception visibility).
#
# send_default_pii=True gives us richer error context (request headers, IP) but
# Shopify requests carry `X-Shopify-Access-Token`. Sentry's default scrubber
# doesn't know about it — `_scrub_shopify_secrets` strips it (plus the OAuth
# `code` query param) before any event leaves the SDK.
#
# We also DROP HTTPException events entirely. Those are intentional error
# responses (502s for external API failures, 400s for validation, etc.) raised
# by route handlers — they're already surfaced to merchants as proper HTTP
# status codes and friendly banners. Sentry should fire on actual bugs
# (AttributeError, KeyError, TypeError, etc.), not on an expected 4xx/5xx.
def _scrub_shopify_secrets(event, hint):
    import re as _re

    # Drop any HTTPException-derived event: handled, expected, not a bug.
    exc_info = (hint or {}).get("exc_info")
    if exc_info and len(exc_info) >= 2:
        from fastapi.exceptions import HTTPException as FastAPIHTTPException
        from starlette.exceptions import HTTPException as StarletteHTTPException
        if isinstance(exc_info[1], (FastAPIHTTPException, StarletteHTTPException)):
            return None  # returning None from before_send drops the event

    request = event.get("request") or {}
    headers = request.get("headers")
    if isinstance(headers, dict):
        for key in list(headers.keys()):
            if key.lower() in ("x-shopify-access-token", "authorization", "cookie", "x-shopify-hmac-sha256"):
                headers[key] = "[Filtered]"
    # OAuth callback URL carries `code=...` — scrub it from query_string
    query = request.get("query_string")
    if isinstance(query, str) and "code=" in query:
        request["query_string"] = _re.sub(r"(?i)\b(code|state|hmac)=[^&]*", r"\1=[Filtered]", query)

    # Scrub DB/Redis URLs from exception values — connection errors can embed
    # the full URL (including password) in the exception message.
    _URL_RE = _re.compile(r"(postgresql|redis|rediss|mysql)(\+\w+)?://[^\s\"']+", _re.I)
    for exc_entry in (event.get("exception") or {}).get("values") or []:
        val = (exc_entry.get("value") or "")
        if _URL_RE.search(val):
            exc_entry["value"] = _URL_RE.sub("[db-url-filtered]", val)

    return event


if settings.sentry_dsn and "pytest" not in sys.modules:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.app_env,
        traces_sample_rate=0.10,
        profiles_sample_rate=0.10,
        send_default_pii=True,
        before_send=_scrub_shopify_secrets,
    )

app = FastAPI(
    title="GiftSense",
    version="0.1.0",
    # Built-in docs disabled in ALL environments — the dev backend is exposed on
    # a public Cloudflare tunnel, so an unauthenticated /docs + /openapi.json
    # would leak the API schema to anyone. Re-exposed below behind admin auth.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/openapi.json", include_in_schema=False)
async def openapi_json(_: str = Depends(require_admin)):
    """OpenAPI schema — admin Basic Auth only (never public)."""
    return app.openapi()


@app.get("/docs", include_in_schema=False)
async def swagger_docs(_: str = Depends(require_admin)):
    """Swagger UI — admin Basic Auth only. The browser reuses the same creds to
    fetch /openapi.json (same origin + realm), so the schema stays gated too."""
    return get_swagger_ui_html(openapi_url="/openapi.json", title="GiftSense API docs")

_origins = ["https://admin.shopify.com"]
if not settings.is_production:
    _origins += ["http://localhost:5173", "http://localhost:3000", "http://localhost:8001"]

# Storefront calls go through the Shopify App Proxy (same-origin to the shop,
# HMAC-signed), so they don't need CORS. The myshopify regex only covers the
# direct-call dev fallback; the real gate is the App Proxy signature check.
_storefront_origin_regex = r"^https://[a-z0-9-]+\.myshopify\.com$"

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_origin_regex=_storefront_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(auth.router)
app.include_router(billing.router)
app.include_router(settings_routes.router)
app.include_router(webhooks.router)


@app.get("/health")
async def health():
    """Real health check: pings DB + Redis. Used by Railway/Cloudflare to
    detect unhealthy instances and route traffic away. Returns 503 when any
    component is down so external orchestrators can react. Component-level
    status is included in the response for log/debug visibility.
    """
    from sqlalchemy import text
    from core.db.session import engine

    components: dict[str, dict] = {}
    overall_ok = True

    # DB ping — `SELECT 1` is the canonical liveness probe. 2s timeout so a
    # stuck DB connection doesn't hang the health endpoint indefinitely.
    try:
        async with asyncio.timeout(2):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        components["database"] = {"status": "ok"}
    except Exception as e:
        components["database"] = {"status": "error", "error": str(e)[:200]}
        overall_ok = False

    # Redis ping — used by ARQ workers + rate limits. Same 2s timeout.
    try:
        import redis.asyncio as redis_asyncio
        async with asyncio.timeout(2):
            client = redis_asyncio.from_url(settings.redis_url)
            try:
                await client.ping()
            finally:
                await client.close()
        components["redis"] = {"status": "ok"}
    except Exception as e:
        components["redis"] = {"status": "error", "error": str(e)[:200]}
        overall_ok = False

    payload = {
        "status": "ok" if overall_ok else "degraded",
        "app": "giftsense",
        "env": settings.app_env,
        "components": components,
    }
    status_code = 200 if overall_ok else 503
    return JSONResponse(payload, status_code=status_code)


_DIST = os.path.join(os.path.dirname(__file__), "..", "dashboard", "dist")
_INDEX = os.path.join(_DIST, "index.html")

_API_PREFIXES = ("/api/", "/auth", "/admin", "/billing", "/webhooks", "/health", "/debug", "/docs", "/assets")

if os.path.isdir(_DIST):
    app.mount("/assets", StaticFiles(directory=os.path.join(_DIST, "assets")), name="assets")

    @app.exception_handler(404)
    async def spa_404(request: Request, exc: HTTPException):
        path = request.url.path
        # Real API paths return JSON 404
        if any(path.startswith(p) for p in _API_PREFIXES):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        # Root-level static files (favicon.svg, icons.svg, etc.)
        file_path = os.path.join(_DIST, path.lstrip("/"))
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        # All React Router paths → SPA entry point
        return FileResponse(_INDEX)

    @app.get("/")
    async def root(request: Request):
        params = dict(request.query_params)
        if "shop" in params and not params.get("embedded"):
            return RedirectResponse(f"/auth?{request.url.query}")
        return FileResponse(_INDEX)
else:
    @app.get("/")
    async def root(request: Request):
        params = dict(request.query_params)
        if "shop" in params and not params.get("embedded"):
            return RedirectResponse(f"/auth?{request.url.query}")
        return {"message": "GiftSense API — run `npm run build` in dashboard/ to serve the frontend"}
