"""GET /api/registries: the dashboard's Registries page (Pro). Totals (lists,
items wanted/bought, guest views, orders and sales from shared lists) and the
newest registries. Owners are shown by their registry name only; we hold no
names or emails, just Shopify's customer id.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import registry as reg
from core.db.models import GiftOrder, Registry, RegistryItem, Shop
from core.db.session import get_db
from core.shopify_deps import get_current_shop

router = APIRouter()

RECENT = 25


@router.get("/api/registries")
async def registries_overview(shop: Shop = Depends(get_current_shop), db: AsyncSession = Depends(get_db)):
    items = (await db.execute(select(
        func.count(RegistryItem.id), func.coalesce(func.sum(RegistryItem.wanted_qty), 0),
        func.coalesce(func.sum(RegistryItem.bought_qty), 0),
    ).where(RegistryItem.shop_id == shop.id))).one()
    lists, views = (await db.execute(select(func.count(Registry.id), func.coalesce(func.sum(Registry.views), 0))
                                     .where(Registry.shop_id == shop.id))).one()
    orders, revenue = (await db.execute(select(
        func.count(GiftOrder.id), func.coalesce(func.sum(GiftOrder.registry_revenue), 0),
    ).where(GiftOrder.shop_id == shop.id, GiftOrder.registry_id.is_not(None)))).one()

    per = select(RegistryItem.registry_id, func.count(RegistryItem.id).label("n"),
                 func.coalesce(func.sum(RegistryItem.wanted_qty), 0).label("wanted"),
                 func.coalesce(func.sum(RegistryItem.bought_qty), 0).label("bought")) \
        .where(RegistryItem.shop_id == shop.id).group_by(RegistryItem.registry_id).subquery()
    rows = (await db.execute(
        select(Registry, per.c.n, per.c.wanted, per.c.bought).outerjoin(per, per.c.registry_id == Registry.id)
        .where(Registry.shop_id == shop.id).order_by(Registry.created_at.desc()).limit(RECENT)
    )).all()
    currency = (await db.execute(select(GiftOrder.currency).where(GiftOrder.shop_id == shop.id)
                                 .order_by(GiftOrder.created_at.desc()).limit(1))).scalar_one_or_none()
    return {
        "available": reg.available(shop),
        "totals": {"registries": lists, "items": items[0], "wanted": int(items[1]), "bought": int(items[2]),
                   "views": int(views), "orders": orders, "revenue": float(revenue), "currency": currency or "USD"},
        "registries": [{
            "title": r.title, "occasion": reg.OCCASIONS.get(r.occasion, "Wish list"),
            "event_date": r.event_date.isoformat() if r.event_date else None,
            "items": n or 0, "wanted": int(w or 0), "bought": int(b or 0), "views": r.views or 0,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r, n, w, b in rows],
    }
