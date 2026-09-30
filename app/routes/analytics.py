"""GET /api/analytics: basic analytics for the dashboard (all plans).

Computed from our own tables (no web pixel), last 30 days:
  sessions             distinct widget sessions that opened the finder (gift_events)
  searched_sessions    sessions that ran an AI search (gift_sessions)
  gift_orders          orders carrying GiftSense gift data (gift_orders)
  attributed_orders    gift orders from a finder session (sid)
  all_orders           every order (order_counts_daily)
  note_attach_rate     gift orders with a gift note
  note_acceptance_rate notes that used our draft as-is or edited
Full analytics + the weekly email (Growth+) come later (week 9).
"""
from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.gifting import vocab
from app.services.gifting.rerank import budget_label
from core.db.models import GiftEvent, GiftOrder, GiftSession, OrderCountDaily, Shop
from core.db.session import get_db
from core.shopify_deps import get_current_shop

router = APIRouter()

DAYS = 30
TOP_N = 5


def _rate(part: int, whole: int) -> float | None:
    return min(1.0, part / whole) if whole else None


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@router.get("/api/analytics")
async def analytics(shop: Shop = Depends(get_current_shop), db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=DAYS)

    sessions = (await db.execute(select(func.count(func.distinct(GiftEvent.sid))).where(
        GiftEvent.shop_id == shop.id, GiftEvent.type == "widget_open", GiftEvent.created_at >= since))).scalar() or 0
    searched = (await db.execute(select(GiftSession.intake).where(
        GiftSession.shop_id == shop.id, GiftSession.searches > 0, GiftSession.created_at >= since))).scalars().all()
    orders = (await db.execute(select(GiftOrder).where(
        GiftOrder.shop_id == shop.id, GiftOrder.created_at >= since))).scalars().all()
    all_orders = (await db.execute(select(func.coalesce(func.sum(OrderCountDaily.orders), 0)).where(
        OrderCountDaily.shop_id == shop.id, OrderCountDaily.day >= since.date()))).scalar() or 0

    attributed = [o for o in orders if o.sid is not None]
    with_note = [o for o in orders if o.note_source]
    accepted = [o for o in with_note if o.note_source in ("ai_accepted", "ai_edited")]

    occasions = Counter((i or {}).get("occasion") for i in searched if (i or {}).get("occasion"))
    budgets = Counter((i or {}).get("budget_band") for i in searched if (i or {}).get("budget_band"))

    by_day: dict = {}
    for o in orders:
        d = _utc(o.created_at).date()
        slot = by_day.setdefault(d, [0, 0.0])
        slot[0] += 1
        slot[1] += float(o.gift_revenue or 0)
    daily = []
    for i in range(DAYS - 1, -1, -1):
        d = (now - timedelta(days=i)).date()
        count, revenue = by_day.get(d, (0, 0.0))
        daily.append({"date": d.isoformat(), "gift_orders": count, "gift_revenue": round(revenue, 2)})

    latest = max(orders, key=lambda o: _utc(o.created_at), default=None)
    return {
        "days": DAYS,
        "currency": latest.currency if latest else "",
        "sessions": sessions,
        "searched_sessions": len(searched),
        "completion_rate": _rate(len(searched), sessions),
        "gift_orders": len(orders),
        "attributed_orders": len(attributed),
        "all_orders": int(all_orders),
        "conversion_rate": _rate(len(attributed), len(searched)),
        "attributed_revenue": round(sum(float(o.order_total or 0) for o in attributed), 2),
        "gift_revenue": round(sum(float(o.gift_revenue or 0) for o in orders), 2),
        "note_attach_rate": _rate(len(with_note), len(orders)),
        "note_acceptance_rate": _rate(len(accepted), len(with_note)),
        "top_occasions": [{"value": k, "label": vocab.LABELS.get(k, k), "count": n}
                          for k, n in occasions.most_common(TOP_N)],
        "top_budgets": [{"value": k, "label": budget_label(k), "count": n}
                        for k, n in budgets.most_common(TOP_N) if k in vocab.BUDGET_BANDS],
        "daily": daily,
    }
