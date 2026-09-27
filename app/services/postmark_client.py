"""Postmark email client — Sales Leak Finder Phase 6 Weekly Digest.

Used by the digest cron to deliver the Monday morning recovery email
to each Pro merchant's OAuth-provided shop/user email. We don't
touch customer emails — this is merchant-facing only (CAN-SPAM
applies to commercial recipient lists; merchant emails count as
business correspondence under their OAuth grant).

Why Postmark:
- Cheap (~$0.001 per email at our v1 volumes — 1 per Pro merchant
  per week is far below the free tier's 100/mo).
- Strict transactional focus (no marketing-email features to
  misuse).
- Strong deliverability reputation.
- Simple REST API with one endpoint per email type.

Dev mode: if POSTMARK_SERVER_TOKEN isn't set in the environment, the
client logs the would-be email contents and returns a fake success
response. Lets the cron run end-to-end in dev without merchants
actually getting emails.

Production setup (PROD_RELEASE_CHECKLIST):
1. Sign up at postmarkapp.com, verify the sending domain
   (prudix.app), set up DKIM + SPF
2. Generate a Server token
3. Set POSTMARK_SERVER_TOKEN in Railway env
4. Set POSTMARK_FROM_EMAIL = "digest@prudix.app"
"""
from __future__ import annotations

import httpx
import structlog
from sqlalchemy import select

from core.config import settings
from core.db.models import SuppressedEmail
from core.db.session import AsyncSessionLocal

logger = structlog.get_logger()

_POSTMARK_API = "https://api.postmarkapp.com/email"


class PostmarkError(Exception):
    """Raised on Postmark API failures so the cron can record the
    error on the digest row and retry next week."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class PostmarkSuppressed(PostmarkError):
    """Raised when the recipient is in `suppressed_emails` (populated by
    POST /webhooks/postmark from Postmark's Bounce + SpamComplaint events).

    Subclasses PostmarkError so every existing `except PostmarkError`
    block records it on the digest row's `send_error` field without
    needing an update. Handlers that want to distinguish "the address
    is dead, stop trying" from "Postmark had a hiccup" can catch this
    subclass first.
    """

    def __init__(self, email: str, reason: str):
        super().__init__(f"Recipient {email} is suppressed ({reason})")
        self.email = email
        self.reason = reason


async def _is_suppressed(email: str) -> tuple[bool, str | None]:
    """Look up `email` (case-insensitive) in the local suppression list.

    Opens its own AsyncSession so callers don't have to plumb a session
    through — this runs on the send path (cron workers, admin preview)
    where a request-scoped session isn't always available.
    """
    lowered = email.strip().lower()
    if not lowered:
        return False, None
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(SuppressedEmail).where(SuppressedEmail.email == lowered)
            )).scalar_one_or_none()
    except Exception as e:  # noqa: BLE001 — DB glitch shouldn't block sends
        logger.warning("postmark_suppression_check_failed", email=lowered, error=str(e))
        return False, None
    if row is None:
        return False, None
    return True, row.reason


async def send_email(
    *,
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str,
    tag: str | None = None,
    from_email: str | None = None,
    reply_to: str | None = None,
) -> dict:
    """Send one transactional email via Postmark.

    Returns the Postmark response dict on success; raises PostmarkError
    on failure so the caller can persist the error.

    Dev mode (no POSTMARK_SERVER_TOKEN): logs + returns a fake
    response. Used both in `arq` runs locally and in pytest.
    """
    # Read via pydantic Settings (loads .env), NOT os.getenv — pydantic
    # does not sync into os.environ, so os.getenv silently returned ""
    # even when the token was set in .env and dev-mode fallback fired.
    server_token = settings.postmark_server_token or ""
    from_addr = from_email or settings.postmark_from_email or "digest@prudix.app"

    # Skip the Postmark API entirely for addresses we've already learned
    # are dead (hard bounce) or hostile (spam complaint). Postmark would
    # 422 the request anyway once its server-side suppression fires, but
    # bailing here keeps the error logs clean and preserves the daily
    # send quota.
    suppressed, suppression_reason = await _is_suppressed(to_email)
    if suppressed:
        raise PostmarkSuppressed(to_email, suppression_reason or "unknown")

    if not server_token:
        logger.info(
            "postmark_dev_mode_send",
            to=to_email, subject=subject, tag=tag,
            preview=text_body[:200],
        )
        return {
            "dev_mode":  True,
            "to":        to_email,
            "subject":   subject,
            "message_id": "dev-mode-no-send",
        }

    payload = {
        "From":          from_addr,
        "To":            to_email,
        "Subject":       subject,
        "HtmlBody":      html_body,
        "TextBody":      text_body,
        "MessageStream": "outbound",
    }
    if tag:
        payload["Tag"] = tag
    if reply_to:
        payload["ReplyTo"] = reply_to

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                _POSTMARK_API,
                headers={
                    "Accept":            "application/json",
                    "Content-Type":      "application/json",
                    "X-Postmark-Server-Token": server_token,
                },
                json=payload,
            )
    except Exception as e:  # noqa: BLE001
        raise PostmarkError(f"Couldn't reach Postmark: {e}") from e

    if resp.status_code in (401, 422):
        raise PostmarkError(
            f"Postmark rejected the send: {resp.text[:200]}",
            status_code=resp.status_code,
        )
    if resp.status_code >= 400:
        raise PostmarkError(
            f"Postmark returned {resp.status_code}: {resp.text[:200]}",
            status_code=resp.status_code,
        )

    return resp.json()


__all__ = ["PostmarkError", "PostmarkSuppressed", "send_email"]
