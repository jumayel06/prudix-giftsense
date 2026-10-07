"""Internal heads-up email to the Prudix team when a merchant approves billing.

Sent once per first subscription approval (new merchant, or a returning one after
uninstall) — not on plan changes or renewals, and not at install time (an install
that never picks a plan is not a customer). Fire-and-forget: never delays or fails
billing. Called from both activation paths: `/billing/callback` and the
`app_subscriptions/update` webhook (whichever arrives first sees the shop still
pending; the other sees it already live and sends nothing).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from html import escape

import structlog

from app.config import PLANS
from app.services.postmark_client import send_email
from core.config import settings

logger = structlog.get_logger()

# Strong references so a pending send isn't garbage-collected mid-flight.
_pending: set[asyncio.Task] = set()

# Fallback "already emailed" memory when Redis isn't configured (per process).
_claimed: dict[str, float] = {}
_CLAIM_TTL_S = 24 * 3600


async def _claim(key: str) -> bool:
    """True the first time `key` is seen. The billing callback and the
    app_subscriptions/update webhook can both decide "first approval" at the
    same instant (no row lock), so the email is claimed once per
    (shop, charge). Redis SET NX across workers when configured; else
    in-process. Fails open (sends) if Redis errors."""
    import time as _time
    if settings.redis_url:
        try:
            import redis.asyncio as redis_asyncio
            r = redis_asyncio.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
            try:
                return bool(await r.set(f"team_email:{key}", "1", nx=True, ex=_CLAIM_TTL_S))
            finally:
                await r.aclose()
        except Exception as e:  # noqa: BLE001
            logger.warning("subscription_notify_claim_failed", key=key, error=str(e))
            return True
    now = _time.time()
    for k in [k for k, t in _claimed.items() if now - t > _CLAIM_TTL_S]:
        _claimed.pop(k, None)
    if key in _claimed:
        return False
    _claimed[key] = now
    return True

FIRST_ACTIVATION_EVENTS = ("trial_started", "activated")


async def _send(shop_domain: str, owner_email: str | None, timezone_name: str | None,
                plan: str, trial: bool, annual: bool, returning: bool,
                charge_id: str | None = None) -> None:
    to_email = settings.install_notify_email
    if not to_email:
        return
    if charge_id and not await _claim(f"{shop_domain}:{charge_id}"):
        logger.info("subscription_notify_duplicate_skipped", shop=shop_domain, charge_id=charge_id)
        return
    plan_name = PLANS.get(plan, {}).get("name", plan.title())
    terms = ("free trial" if trial else "paid") + (", annual" if annual else ", monthly")
    kind = "Returning merchant subscribed" if returning else "New merchant subscribed"
    env_tag = "" if settings.is_production else f"[{settings.app_env}] "
    rows = [
        ("Store", shop_domain),
        ("Plan", f"{plan_name} ({terms})"),
        ("Owner email", owner_email or "unknown"),
        ("Timezone", timezone_name or "unknown"),
        ("When", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
    ]
    html_rows = "".join(
        f'<tr><td style="padding:4px 16px 4px 0;color:#64748b;">{escape(k)}</td>'
        f'<td style="padding:4px 0;color:#0f172a;">{escape(v)}</td></tr>'
        for k, v in rows
    )
    html_body = (
        f'<div style="font-family:-apple-system,Segoe UI,sans-serif;font-size:15px;">'
        f'<h3 style="margin:0 0 12px;">{escape(kind)}: {escape(shop_domain)}</h3>'
        f"<table>{html_rows}</table></div>"
    )
    text_body = f"{kind}: {shop_domain}\n" + "\n".join(f"{k}: {v}" for k, v in rows)
    try:
        await send_email(
            to_email=to_email,
            subject=f"{env_tag}{kind}: {shop_domain} ({plan_name})",
            html_body=html_body,
            text_body=text_body,
            tag="subscription-notify",
        )
    except Exception as e:  # noqa: BLE001 — a notification must never affect billing
        logger.warning("subscription_notify_failed", shop=shop_domain, error=str(e))


def notify_first_subscription(shop_record, *, plan: str, trial: bool, annual: bool, returning: bool,
                              charge_id: str | None = None) -> None:
    """Schedule the email. Copies plain values first so the task never touches
    the request's DB session. Safe to call from any async context; never raises."""
    try:
        if not settings.install_notify_email:
            return
        task = asyncio.get_running_loop().create_task(_send(
            shop_record.shop_domain, shop_record.shop_owner_email, shop_record.store_timezone,
            plan, trial, annual, returning, charge_id,
        ))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    except Exception as e:  # noqa: BLE001
        logger.warning("subscription_notify_schedule_failed", error=str(e))
