"""GET /api/gift-orders: the dashboard's Gift orders list (all plans).

Reads our own gift_orders rows (order facts only: no customer, no note text),
newest first, 25 per page. Each row links to the Shopify order, where
Print → GiftSense gift cards prints cards and receipts.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models import GiftOrder, Shop
from core.db.session import get_db
from core.shopify_deps import get_current_shop

router = APIRouter()

PAGE_SIZE = 25


def _row(o: GiftOrder) -> dict:
    groups = o.groups or []
    return {
        "order_id": o.order_id,
        "order_name": o.order_name,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "mode": o.delivery_mode,
        "gift_lines": o.gift_lines,
        "gift_revenue": float(o.gift_revenue or 0),
        "order_total": float(o.order_total or 0),
        "currency": o.currency,
        "recipients": [g["label"] for g in groups if g.get("label")],
        "wraps": sorted({g["wrap"] for g in groups if g.get("wrap")}),
        "has_note": o.note_source is not None,
        "note_source": o.note_source,
        "from_finder": o.sid is not None,
        "annotated": bool(o.annotated),
    }


@router.get("/api/gift-orders")
async def list_gift_orders(
    page: int = Query(1, ge=1, le=1000),
    shop: Shop = Depends(get_current_shop),
    db: AsyncSession = Depends(get_db),
):
    total = (await db.execute(select(func.count()).select_from(GiftOrder).where(
        GiftOrder.shop_id == shop.id))).scalar_one()
    rows = (await db.execute(select(GiftOrder).where(GiftOrder.shop_id == shop.id)
                             .order_by(GiftOrder.created_at.desc(), GiftOrder.order_id.desc())
                             .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))).scalars().all()
    return {"orders": [_row(o) for o in rows], "page": page, "page_size": PAGE_SIZE, "total": total,
            "has_next": page * PAGE_SIZE < total}
