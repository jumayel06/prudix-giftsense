"""Sign-in for the internal admin dashboard: password + TOTP, signed session cookie.

Replaces HTTP Basic Auth (unreliable on mobile: iOS auto-capitalises the
username, password managers can't fill the native prompt, in-app browsers drop
it; and it had no attempt limit, no 2FA and no logout).

- Login form at `{admin_path}/login` (see app/admin/login.py).
- Password (+ 6-digit TOTP code when ADMIN_TOTP_SECRET is set).
- On success: HMAC-signed, HttpOnly, Secure, SameSite=Lax cookie, 12h expiry.
  The signing key is derived from TOKEN_ENCRYPTION_KEY + the admin password +
  the TOTP secret, so rotating either credential signs everyone out.
- Failed attempts are limited per client IP (Redis when configured, else
  in-process), and a TOTP code can't be reused.

`require_admin` keeps its name and signature so existing dependency overrides
in tests keep working. Never use this for merchant-facing routes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import struct
import time
from urllib.parse import quote

import structlog
from fastapi import HTTPException, Request, status

from core.config import settings

logger = structlog.get_logger()

COOKIE_NAME = "giftsense_admin"
SESSION_SECONDS = 12 * 3600

MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 15 * 60

TOTP_STEP = 30
TOTP_DIGITS = 6
TOTP_DRIFT_STEPS = 1  # accept the previous/next 30s window (phone clock skew)

_ADMIN_PATH_RE = re.compile(r"^/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$")


# ── Admin path ───────────────────────────────────────────────────────────────

def admin_path() -> str:
    """Normalised ADMIN_PATH (e.g. "/ops-7f3k9q"). Invalid values fall back to
    "/admin" rather than failing boot."""
    raw = (settings.admin_path or "/admin").strip().rstrip("/")
    if not _ADMIN_PATH_RE.match(raw):
        logger.error("admin_path_invalid_falling_back", value=settings.admin_path)
        return "/admin"
    return raw


def is_admin_tool_path(path: str) -> bool:
    base = admin_path()
    return path == base or path.startswith(base + "/")


def safe_next(target: str | None) -> str:
    """Post-login redirect target: only our own admin pages (or /docs), never an
    external URL."""
    base = admin_path()
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        path_only = target.split("?", 1)[0]
        if is_admin_tool_path(path_only) or path_only in ("/docs", "/openapi.json"):
            if not path_only.startswith(base + "/login"):
                return target
    return base + "/"


# ── TOTP (RFC 6238, SHA-1, 6 digits, 30s) ────────────────────────────────────

def _totp_key(secret: str) -> bytes:
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def totp_at(secret: str, counter: int) -> str:
    digest = hmac.new(_totp_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** TOTP_DIGITS)
    return str(code).zfill(TOTP_DIGITS)


def totp_match(secret: str, code: str, now: float | None = None) -> int | None:
    """Return the matching time-step counter, or None."""
    code = (code or "").strip().replace(" ", "")
    if not (code.isdigit() and len(code) == TOTP_DIGITS):
        return None
    current = int((now if now is not None else time.time()) // TOTP_STEP)
    for counter in range(current - TOTP_DRIFT_STEPS, current + TOTP_DRIFT_STEPS + 1):
        if hmac.compare_digest(totp_at(secret, counter), code):
            return counter
    return None


def totp_enabled() -> bool:
    return bool(settings.admin_totp_secret.strip())


# ── Signed session cookie ────────────────────────────────────────────────────

def _signing_key() -> bytes:
    material = "|".join([
        "giftsense-admin-session-v1",
        settings.token_encryption_key,
        settings.internal_admin_password,
        settings.admin_totp_secret.strip(),
    ])
    return hashlib.sha256(material.encode("utf-8")).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def make_session(username: str, now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    body = _b64(json.dumps({"u": username, "iat": issued, "exp": issued + SESSION_SECONDS},
                           separators=(",", ":")).encode("utf-8"))
    sig = _b64(hmac.new(_signing_key(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_session(token: str | None, now: float | None = None) -> str | None:
    """Username from a valid, unexpired session token; None otherwise."""
    if not token or token.count(".") != 1:
        return None
    body, sig = token.split(".")
    expected = _b64(hmac.new(_signing_key(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    if int(data.get("exp", 0)) <= int(now if now is not None else time.time()):
        return None
    if data.get("u") != settings.internal_admin_username:
        return None
    return data["u"]


def cookie_secure() -> bool:
    return settings.app_env not in ("development", "test")


# ── Attempt limiting + TOTP replay guard ─────────────────────────────────────

class _MemoryStore:
    def __init__(self):
        self._data: dict[str, tuple[int, float]] = {}

    async def incr(self, key: str, ttl: int) -> int:
        now = time.time()
        count, expires = self._data.get(key, (0, now + ttl))
        if expires <= now:
            count, expires = 0, now + ttl
        self._data[key] = (count + 1, expires)
        return count + 1

    async def get_int(self, key: str) -> int:
        count, expires = self._data.get(key, (0, 0))
        return count if expires > time.time() else 0

    async def set_int(self, key: str, value: int, ttl: int) -> None:
        self._data[key] = (value, time.time() + ttl)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)


class _RedisStore:
    def __init__(self, url: str):
        import redis.asyncio as redis_asyncio
        self._r = redis_asyncio.from_url(url, socket_connect_timeout=2, socket_timeout=2)

    async def incr(self, key: str, ttl: int) -> int:
        count = await self._r.incr(key)
        if count == 1:
            await self._r.expire(key, ttl)
        return int(count)

    async def get_int(self, key: str) -> int:
        v = await self._r.get(key)
        return int(v) if v is not None else 0

    async def set_int(self, key: str, value: int, ttl: int) -> None:
        await self._r.set(key, value, ex=ttl)

    async def delete(self, key: str) -> None:
        await self._r.delete(key)


_memory = _MemoryStore()
_store = None


def _get_store():
    global _store
    if _store is None:
        _store = _RedisStore(settings.redis_url) if settings.redis_url else _memory
    return _store


async def _safe(op, *args, default=0):
    """Run a store op; on a Redis outage fall back to the in-process store
    (limits then apply per web worker — degraded, never wide open or locked)."""
    store = _get_store()
    try:
        return await getattr(store, op)(*args)
    except Exception as e:  # noqa: BLE001
        if store is _memory:
            return default
        logger.warning("admin_auth_store_fallback", op=op, error=str(e))
        return await getattr(_memory, op)(*args)


def client_ip(request: Request) -> str:
    """Rightmost X-Forwarded-For entry = the address Railway's edge saw (the
    leftmost entries are client-supplied and spoofable). Falls back to the
    socket peer."""
    xff = request.headers.get("x-forwarded-for", "")
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    if parts:
        return parts[-1]
    return request.client.host if request.client else "unknown"


def _fail_key(ip: str) -> str:
    return f"admin_auth:fail:{ip}"


async def is_locked_out(ip: str) -> bool:
    return await _safe("get_int", _fail_key(ip)) >= MAX_FAILURES


async def record_failure(ip: str) -> int:
    return await _safe("incr", _fail_key(ip), FAILURE_WINDOW_SECONDS)


async def clear_failures(ip: str) -> None:
    await _safe("delete", _fail_key(ip), default=None)


async def consume_totp_counter(counter: int) -> bool:
    """False if this time-step (or a later one) was already used — blocks
    replaying a code someone saw over your shoulder."""
    key = "admin_auth:totp_last"
    last = await _safe("get_int", key)
    if counter <= last:
        return False
    await _safe("set_int", key, counter, TOTP_STEP * (2 * TOTP_DRIFT_STEPS + 2), default=None)
    return True


# ── Credential check + route dependency ──────────────────────────────────────

def credentials_ok(username: str, password: str) -> bool:
    user_ok = secrets.compare_digest(username.encode("utf-8"), settings.internal_admin_username.encode("utf-8"))
    pass_ok = secrets.compare_digest(password.encode("utf-8"), settings.internal_admin_password.encode("utf-8"))
    return user_ok and pass_ok and bool(settings.internal_admin_username)


def _ensure_configured() -> None:
    if not settings.internal_admin_password or not settings.internal_admin_username:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin dashboard is not configured (INTERNAL_ADMIN_USERNAME / INTERNAL_ADMIN_PASSWORD missing).",
        )


def require_admin(request: Request) -> str:
    """Dependency for every admin page. Signed-in → username. Otherwise a GET is
    sent to the login page (and back afterwards); anything else gets 401."""
    _ensure_configured()
    user = read_session(request.cookies.get(COOKIE_NAME))
    if user:
        return user
    if request.method == "GET":
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="Sign in required",
            headers={"Location": f"{admin_path()}/login?next={quote(target, safe='')}"},
        )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in required")
