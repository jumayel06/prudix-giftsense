"""Customer-level GDPR handling for Shopify's mandatory privacy webhooks.

Ported from Prudix Commerce (commit 6a0a6a7), where `customers/redact` and
`customers/data_request` had been no-ops claiming "no PII". GiftSense holds no
customer name / email / phone / address, but Shopify counts customer IDs and
order IDs as personal data, so every row keyed by them must be deletable and
reportable.

Each GiftSense table keyed by customer or order ID is registered in
`_tables()` (gift_orders now; gift_media, choice_requests, registries…
as they ship) and covered by tests. gift_sessions have no order id; they are
reached through the redacted orders' `sid` (`_linked_session_sids`).

`customers/redact` deletes the rows for the customer + `orders_to_redact`.
`customers/data_request` collects them and emails the store owner, who answers
the shopper (Shopify doesn't read the webhook response body).
"""

import html
import json
import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

def _tables():
    from core.db.models import GiftOrder
    # (model, customer_id column name or None, order_id column name or None)
    return [(GiftOrder, None, "order_id")]


async def _linked_session_sids(db, shop_id, order_ids: list[str]) -> list:
    """Widget sessions behind the redacted orders (attribution via sid)."""
    from sqlalchemy import select
    from core.db.models import GiftOrder
    if not order_ids:
        return []
    rows = await db.execute(select(GiftOrder.sid).where(
        GiftOrder.shop_id == shop_id, GiftOrder.order_id.in_(order_ids), GiftOrder.sid.is_not(None)))
    return [r for r in rows.scalars() if r]


def customer_and_order_ids(payload: dict) -> tuple[str | None, list[str]]:
    customer_id = (payload.get("customer") or {}).get("id")
    orders = payload.get("orders_to_redact") or payload.get("orders_requested") or []
    return (str(customer_id) if customer_id else None), [str(o) for o in orders if o]


def _conditions(model, customer_col, order_col, customer_id, order_ids):
    from sqlalchemy import or_
    conds = []
    if customer_col and customer_id:
        conds.append(getattr(model, customer_col) == customer_id)
    if order_col and order_ids:
        conds.append(getattr(model, order_col).in_(order_ids))
    return or_(*conds) if conds else None


async def redact_customer(shop_id: uuid.UUID, payload: dict, db: AsyncSession) -> dict:
    """Delete every row tied to the payload's customer + orders. Returns counts."""
    from sqlalchemy import delete
    from core.db.models import GiftSession
    customer_id, order_ids = customer_and_order_ids(payload)
    counts: dict[str, int] = {}
    sids = await _linked_session_sids(db, shop_id, order_ids)
    if sids:
        res = await db.execute(delete(GiftSession).where(GiftSession.shop_id == shop_id, GiftSession.sid.in_(sids)))
        counts["gift_sessions"] = res.rowcount or 0
    for model, customer_col, order_col in _tables():
        cond = _conditions(model, customer_col, order_col, customer_id, order_ids)
        if cond is None:
            continue
        res = await db.execute(delete(model).where(model.shop_id == shop_id, cond))
        counts[model.__tablename__] = res.rowcount or 0
    await db.commit()
    return counts


async def collect_customer_data(shop_id: uuid.UUID, payload: dict, db: AsyncSession) -> dict:
    """Everything GiftSense holds for the payload's customer + requested orders."""
    from sqlalchemy import select
    from core.db.models import GiftSession
    customer_id, order_ids = customer_and_order_ids(payload)
    data: dict[str, list[dict]] = {}
    tables = list(_tables())
    sids = await _linked_session_sids(db, shop_id, order_ids)
    for model, customer_col, order_col in tables:
        cond = _conditions(model, customer_col, order_col, customer_id, order_ids)
        if cond is None:
            continue
        rows = (await db.execute(select(model).where(model.shop_id == shop_id, cond))).scalars().all()
        cols = [c.name for c in model.__table__.columns if c.name not in ("id", "shop_id")]
        data[model.__tablename__] = [_row(r, cols) for r in rows]
    if sids:
        rows = (await db.execute(select(GiftSession).where(GiftSession.shop_id == shop_id,
                                                            GiftSession.sid.in_(sids)))).scalars().all()
        cols = [c.name for c in GiftSession.__table__.columns if c.name not in ("id", "shop_id")]
        data["gift_sessions"] = [_row(r, cols) for r in rows]
    return {k: v for k, v in data.items() if v}


def _row(r, cols) -> dict:
    return {c: (v if v is None or isinstance(v, (int, float, str, bool, list, dict)) else str(v))
            for c in cols for v in [getattr(r, c, None)]}


def render_data_request_email(shop_domain: str, payload: dict, data: dict) -> tuple[str, str, str]:
    """(subject, html_body, text_body) for the store-owner data-request email."""
    customer_id, _ = customer_and_order_ids(payload)
    request_id = (payload.get("data_request") or {}).get("id")
    subject = f"Customer data request — {shop_domain}"
    dump = json.dumps(data, indent=2, default=str) if data else "No data held for this customer."
    intro = (
        f"Shopify forwarded a customer data request (request id {request_id}) for customer "
        f"{customer_id or 'unknown'}. Below is everything Prudix GiftSense holds for this customer. "
        "Please pass it on to the customer. We hold no names, emails, phone numbers or addresses."
    )
    text_body = f"{intro}\n\n{dump}\n"
    html_body = f"<p>{html.escape(intro)}</p><pre>{html.escape(dump)}</pre>"
    return subject, html_body, text_body
