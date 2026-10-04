"""GET /api/home: everything the dashboard Home page shows, in one call.

  summary        last 30 days (sessions, gift orders, finder revenue…) with the
                 30 days before for ▲/▼, and gift orders per day for 14 days
  recent_orders  the 5 newest gift orders (order facts only)
  catalog        products, how many the gift finder has read, last sync
  storefront     whether the gift finder is on in the live theme
  features       the gifting toolkit's state per feature, with plan access
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PLANS
from app.services import analytics, delivery, media, registry, theme_status, wrap
from app.services.gift_settings import NOTE_TONES, note_settings
from core.db.models import CatalogProductRow, CatalogSync, GiftOrder, Registry, Shop
from core.db.session import get_db
from core.shopify_deps import get_current_shop

router = APIRouter()


@router.get("/api/home")
async def home(shop: Shop = Depends(get_current_shop), db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    features = PLANS.get(shop.plan_tier, PLANS["starter"])["features"]
    month = await analytics.summary(db, shop, 30, now)
    before = await analytics.summary(db, shop, 30, now - timedelta(days=30), daily=False)
    keys = ("sessions", "gift_orders", "attributed_revenue", "conversion_rate")

    orders = (await db.execute(select(GiftOrder).where(GiftOrder.shop_id == shop.id)
                               .order_by(GiftOrder.created_at.desc()).limit(5))).scalars().all()
    total, analyzed, excluded = (await db.execute(select(
        func.count(CatalogProductRow.id), func.count(CatalogProductRow.embedding),
        func.count(CatalogProductRow.id).filter(CatalogProductRow.excluded.is_(True)),
    ).where(CatalogProductRow.shop_id == shop.id))).one()
    sync = (await db.execute(select(CatalogSync).where(CatalogSync.shop_id == shop.id)
                             .order_by(CatalogSync.started_at.desc()).limit(1))).scalar_one_or_none()
    check = (shop.gift_settings or {}).get("theme_check") or {}
    w = wrap.wrap_settings(shop)
    d = delivery.delivery_settings(shop)
    registries = (await db.execute(select(func.count(Registry.id)).where(Registry.shop_id == shop.id))).scalar() or 0

    return {
        "store": shop.shop_domain.removesuffix(".myshopify.com"),
        "summary": {**{k: month[k] for k in keys}, "currency": month["currency"] or before["currency"] or "USD",
                    "all_orders": month["all_orders"],
                    "changes": {k: analytics.change(month[k], before[k]) for k in keys},
                    "daily": month["daily"][-14:]},
        "recent_orders": [{
            "order_id": o.order_id, "order_name": o.order_name, "gift_revenue": float(o.gift_revenue or 0),
            "currency": o.currency, "created_at": o.created_at.isoformat() if o.created_at else None,
            "recipients": [g["label"] for g in (o.groups or []) if g.get("label")], "from_finder": o.sid is not None,
            "has_message": any(g.get("message") for g in (o.groups or [])),
            "has_wrap": any(g.get("wrap") for g in (o.groups or [])),
        } for o in orders],
        "catalog": {"products": total, "analyzed": analyzed, "excluded": excluded,
                    "sync_status": sync.status if sync else None,
                    "synced_at": (sync.finished_at or sync.started_at).isoformat() if sync and (sync.finished_at or sync.started_at) else None},
        "storefront": {"embed": check.get("embed", "unknown"), "theme_name": check.get("theme_name"),
                       "warning": theme_status.warning(shop) is not None},
        "features": {
            "notes": {"available": "ai_notes" in features, "on": True,
                      "detail": NOTE_TONES.get(note_settings(shop)["tone"], "")},
            "wrap": {"available": "gift_wrap" in features, "on": bool(w["enabled"] and w["ready"]),
                     "detail": f"{len(w['styles'])} style{'' if len(w['styles']) == 1 else 's'}" if w["styles"] else None},
            "arrive_by": {"available": "arrive_by" in features, "on": bool(d["enabled"]) and "arrive_by" in features,
                          "detail": f"{d['processing_days']}+{d['transit_days']} days" if d["enabled"] else None},
            "messages": {"available": "voice_messages" in features, "on": bool(media.enabled_kinds(shop)),
                         "detail": " + ".join(k.capitalize() for k in media.enabled_kinds(shop)) or None},
            "registries": {"available": registry.available(shop), "on": registry.available(shop),
                           "detail": f"{registries} registr{'y' if registries == 1 else 'ies'}" if registries else None},
            "printing": {"available": True, "on": True, "detail": "Cards + gift receipts"},
        },
    }
