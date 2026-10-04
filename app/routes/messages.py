"""GET /api/messages: the dashboard's Voice & video page (Growth: voice, Pro:
voice + video). This month's usage against the plan's limit, and the recent
recordings that reached an order (kind, order, views, when they're deleted).
Recordings themselves are only playable through the recipient page.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PLANS
from app.services import media
from core.db.models import GiftMedia, GiftOrder, Shop
from core.db.session import get_db
from core.shopify_deps import get_current_shop

router = APIRouter()

RECENT = 25


@router.get("/api/messages")
async def messages_overview(shop: Shop = Depends(get_current_shop), db: AsyncSession = Depends(get_db)):
    features = PLANS.get(shop.plan_tier, PLANS["starter"])["features"]
    rows = (await db.execute(
        select(GiftMedia, GiftOrder.order_name)
        .outerjoin(GiftOrder, (GiftOrder.shop_id == GiftMedia.shop_id) & (GiftOrder.order_id == GiftMedia.order_id))
        .where(GiftMedia.shop_id == shop.id, GiftMedia.status == "linked")
        .order_by(GiftMedia.created_at.desc()).limit(RECENT)
    )).all()
    return {
        "voice": "voice_messages" in features,
        "video": "video_messages" in features,
        "available": bool(media.enabled_kinds(shop)),
        "used": await media.used_this_cycle(db, shop),
        "limit": media.monthly_limit(shop),
        "retention_days": media.MEDIA_RETENTION_DAYS,
        "max_secs": {k: v[1] for k, v in media.KINDS.items()},
        "messages": [{
            "kind": m.kind, "order_id": m.order_id, "order_name": name, "duration_s": m.duration_s,
            "views": m.view_count, "created_at": m.created_at.isoformat() if m.created_at else None,
            "expires_at": m.expires_at.isoformat() if m.expires_at else None,
        } for m, name in rows],
    }
