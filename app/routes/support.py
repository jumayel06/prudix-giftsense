"""Merchant support requests (ported from Prudix Commerce).

POST /api/support/ticket   submit a request (emailed to support@prudix.app)
GET  /api/support/tickets  this shop's own requests

Handled in /admin/support. Everything the merchant typed is HTML-escaped in
the notification email (Commerce's version wasn't).
"""
import html
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import rate_limit
from app.services.postmark_client import PostmarkError, send_email
from core.config import settings
from core.db.models import Shop, SupportTicket
from core.db.session import get_db
from core.shopify_deps import get_current_shop

logger = structlog.get_logger()
router = APIRouter(prefix="/api/support", tags=["support"])

SUPPORT_EMAIL = "support@prudix.app"
CATEGORY_LABELS = {"bug": "Bug report", "billing": "Billing question", "feature": "Feature request", "other": "Other"}
TICKETS_PER_HOUR = 5


class TicketIn(BaseModel):
    category: Literal["bug", "billing", "feature", "other"]
    subject: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=5000)
    screenshot_url: Optional[str] = Field(default=None, max_length=500, pattern=r"^https?://\S+$")


def serialize(t: SupportTicket) -> dict:
    return {"id": str(t.id), "ticket_number": t.ticket_number, "category": t.category, "subject": t.subject,
            "message": t.message, "screenshot_url": t.screenshot_url, "status": t.status,
            "created_at": t.created_at.isoformat() if t.created_at else None}


@router.post("/ticket", status_code=201)
async def submit_ticket(body: TicketIn, shop: Shop = Depends(get_current_shop), db: AsyncSession = Depends(get_db)):
    if not body.subject.strip() or not body.message.strip():
        raise HTTPException(422, detail={"code": "empty", "message": "Please fill in the subject and message."})
    if not await rate_limit.hit(f"support:{shop.id}", TICKETS_PER_HOUR, rate_limit.HOUR):
        raise HTTPException(429, detail={"code": "slow_down",
                                         "message": f"Please email {SUPPORT_EMAIL} if you need to send more right now."})
    number = (await db.execute(select(func.coalesce(func.max(SupportTicket.ticket_number), 1000) + 1))).scalar()
    ticket = SupportTicket(id=uuid.uuid4(), ticket_number=number, shop_id=shop.id, category=body.category,
                           subject=body.subject.strip(), message=body.message.strip(),
                           screenshot_url=(body.screenshot_url or "").strip() or None, status="open",
                           created_at=datetime.now(timezone.utc))
    db.add(ticket)
    await db.commit()
    await notify(ticket, shop)
    logger.info("support_ticket_created", shop=shop.shop_domain, ticket=ticket.ticket_number)
    return serialize(ticket)


@router.get("/tickets")
async def list_tickets(shop: Shop = Depends(get_current_shop), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(SupportTicket).where(SupportTicket.shop_id == shop.id)
                             .order_by(SupportTicket.created_at.desc()))).scalars().all()
    return [serialize(t) for t in rows]


async def notify(ticket: SupportTicket, shop: Shop) -> None:
    """Email support@ (reply-to the store owner). Never fails the request."""
    esc = html.escape
    host = settings.get_app_host()
    admin_url = f"https://{host}/admin/support" if host else "/admin/support"
    label = CATEGORY_LABELS.get(ticket.category, ticket.category)
    owner = shop.shop_owner_email
    rows = [("Store", shop.shop_domain), ("Plan", f"{shop.plan_tier} ({shop.plan_status})"), ("Category", label),
            ("Merchant email", owner or "unknown"), ("Subject", ticket.subject)]
    if ticket.screenshot_url:
        rows.append(("Screenshot", ticket.screenshot_url))
    body = ("<div style=\"font-family:-apple-system,'Segoe UI',sans-serif;max-width:600px\">"
            f"<h2 style=\"margin:0 0 4px\">New GiftSense support ticket #{ticket.ticket_number}</h2>"
            + "".join(f"<p style=\"margin:0 0 8px\"><b>{esc(k)}:</b> {esc(v)}</p>" for k, v in rows)
            + f"<div style=\"white-space:pre-wrap;background:#f8fafc;border-left:3px solid #111827;padding:12px 14px;"
              f"margin:12px 0\">{esc(ticket.message)}</div>"
              f"<p><a href=\"{esc(admin_url)}\">View in admin</a></p></div>")
    text = "\n".join([f"New GiftSense support ticket #{ticket.ticket_number}", *(f"{k}: {v}" for k, v in rows), "",
                      ticket.message, "", f"View in admin: {admin_url}"])
    try:
        await send_email(to_email=SUPPORT_EMAIL, subject=f"[GiftSense #{ticket.ticket_number}] {ticket.subject[:120]} "
                                                          f"({shop.shop_domain})",
                         html_body=body, text_body=text, tag="support-ticket", from_email=SUPPORT_EMAIL,
                         reply_to=owner or None)
    except PostmarkError as e:
        logger.warning("support_ticket_email_failed", ticket=ticket.ticket_number, error=str(e)[:200])
