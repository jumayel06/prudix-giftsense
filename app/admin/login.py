"""Admin sign-in / sign-out pages (not behind require_admin)."""
from __future__ import annotations

import os

import structlog
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.admin import auth
from core.config import settings

logger = structlog.get_logger()

router = APIRouter(tags=["admin"], include_in_schema=False)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

_GENERIC_ERROR = "Those details didn't match. Check them and try again."


def _page(request: Request, next_target: str, error: str | None = None, status_code: int = 200):
    return templates.TemplateResponse(
        request, "login.html",
        {"admin_path": auth.admin_path(), "next": next_target, "error": error,
         "totp_enabled": auth.totp_enabled()},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = ""):
    auth._ensure_configured()
    target = auth.safe_next(next)
    if auth.read_session(request.cookies.get(auth.COOKIE_NAME)):
        return RedirectResponse(target, status_code=303)
    return _page(request, target)


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    code: str = Form(""),
    next: str = Form(""),
):
    auth._ensure_configured()
    target = auth.safe_next(next)
    ip = auth.client_ip(request)

    if await auth.is_locked_out(ip):
        logger.warning("admin_login_locked_out", ip=ip)
        minutes = auth.FAILURE_WINDOW_SECONDS // 60
        return _page(request, target, f"Too many attempts. Try again in {minutes} minutes.", 429)

    ok = auth.credentials_ok(username.strip(), password)
    if ok and auth.totp_enabled():
        counter = auth.totp_match(settings.admin_totp_secret, code)
        ok = counter is not None and await auth.consume_totp_counter(counter)

    if not ok:
        attempts = await auth.record_failure(ip)
        logger.warning("admin_login_failed", ip=ip, attempts=attempts)
        return _page(request, target, _GENERIC_ERROR, 401)

    await auth.clear_failures(ip)
    logger.info("admin_login_ok", ip=ip)
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(
        auth.COOKIE_NAME, auth.make_session(settings.internal_admin_username),
        max_age=auth.SESSION_SECONDS, path="/", httponly=True,
        secure=auth.cookie_secure(), samesite="lax",
    )
    return resp


@router.post("/logout")
async def logout():
    resp = RedirectResponse(f"{auth.admin_path()}/login", status_code=303)
    resp.delete_cookie(auth.COOKIE_NAME, path="/", httponly=True, secure=auth.cookie_secure(), samesite="lax")
    return resp
