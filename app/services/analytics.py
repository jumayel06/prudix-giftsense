"""Gift analytics from our own tables (docs/TECHNICAL_PLAN.md §7.1; no web pixel).

`summary(db, shop, days, end)` covers the `days` before `end`:
  sessions             distinct widget sessions that opened the finder (gift_events)
  searched_sessions    sessions that ran an AI search (gift_sessions)
  gift_orders          orders carrying GiftSense gift data (gift_orders)
  attributed_orders    gift orders from a finder session (sid)
  all_orders           every order (order_counts_daily)
  note_attach_rate     gift orders with a gift note
  note_acceptance_rate notes that used our draft as-is or edited

`full=True` (Growth+) adds the breakdowns: wrap / message / arrive-by attach
rates, direct vs "to me" orders, gifts per order, top recipients, registry
sales. The dashboard compares a period with the one before it; the weekly
email (app/workers/digest.py) uses the same numbers for the last 7 days.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.gifting import vocab
from app.services.gifting.rerank import budget_label
from core.db.models import GiftEvent, GiftMedia, GiftOrder, GiftSession, OrderCountDaily, Shop

TOP_N = 5


def _rate(part: int, whole: int) -> float | None:
    return min(1.0, part / whole) if whole else None


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def summary(db: AsyncSession, shop: Shop, days: int, end: datetime | None = None, *,
                  full: bool = False, daily: bool = True) -> dict:
    end = end or datetime.now(timezone.utc)
    since = end - timedelta(days=days)

    sessions = (await db.execute(select(func.count(func.distinct(GiftEvent.sid))).where(
        GiftEvent.shop_id == shop.id, GiftEvent.type == "widget_open",
        GiftEvent.created_at >= since, GiftEvent.created_at < end))).scalar() or 0
    searched = (await db.execute(select(GiftSession.intake).where(
        GiftSession.shop_id == shop.id, GiftSession.searches > 0,
        GiftSession.created_at >= since, GiftSession.created_at < end))).scalars().all()
    orders = (await db.execute(select(GiftOrder).where(
        GiftOrder.shop_id == shop.id, GiftOrder.created_at >= since, GiftOrder.created_at < end))).scalars().all()
    all_orders = (await db.execute(select(func.coalesce(func.sum(OrderCountDaily.orders), 0)).where(
        OrderCountDaily.shop_id == shop.id, OrderCountDaily.day >= since.date(),
        OrderCountDaily.day < end.date() + timedelta(days=1)))).scalar() or 0

    attributed = [o for o in orders if o.sid is not None]
    with_note = [o for o in orders if o.note_source]
    accepted = [o for o in with_note if o.note_source in ("ai_accepted", "ai_edited")]
    occasions = Counter((i or {}).get("occasion") for i in searched if (i or {}).get("occasion"))
    budgets = Counter((i or {}).get("budget_band") for i in searched if (i or {}).get("budget_band"))
    latest = max(orders, key=lambda o: _utc(o.created_at), default=None)

    out = {
        "days": days,
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
    }
    if daily:
        by_day: dict = {}
        for o in orders:
            slot = by_day.setdefault(_utc(o.created_at).date(), [0, 0.0])
            slot[0] += 1
            slot[1] += float(o.gift_revenue or 0)
        out["daily"] = []
        for i in range(days - 1, -1, -1):
            d = (end - timedelta(days=i)).date()
            count, revenue = by_day.get(d, (0, 0.0))
            out["daily"].append({"date": d.isoformat(), "gift_orders": count, "gift_revenue": round(revenue, 2)})
    if full:
        out.update(await _breakdowns(db, shop, orders, searched))
    return out


async def _breakdowns(db: AsyncSession, shop: Shop, orders: list[GiftOrder], searched: list) -> dict:
    n = len(orders)
    groups = [g for o in orders for g in (o.groups or [])]
    with_wrap = [o for o in orders if any(g.get("wrap") for g in (o.groups or []))]
    with_message = [o for o in orders if any(g.get("message") for g in (o.groups or []))]
    order_ids = [o.order_id for o in orders]
    kinds = Counter()
    if order_ids:
        kinds = Counter((await db.execute(select(GiftMedia.kind).where(
            GiftMedia.shop_id == shop.id, GiftMedia.order_id.in_(order_ids)))).scalars())
    recipients = Counter((i or {}).get("recipient") for i in searched if (i or {}).get("recipient"))
    registry = [o for o in orders if o.registry_id is not None]
    self_orders = [o for o in orders if o.delivery_mode == "self"]
    return {
        "full": True,
        "wrap_attach_rate": _rate(len(with_wrap), n),
        "message_attach_rate": _rate(len(with_message), n),
        "messages": {"voice": kinds.get("voice", 0), "video": kinds.get("video", 0)},
        "arrive_by_rate": _rate(len([o for o in orders if o.arrive_by]), n),
        "delivery_mix": {"direct": len([o for o in orders if o.delivery_mode == "direct"]), "self": len(self_orders)},
        "gifts_per_order": round(len([g for g in groups if g.get("id") != "order"]) / len(self_orders), 2)
        if self_orders else None,
        "top_recipients": [{"value": k, "label": vocab.LABELS.get(k, k), "count": c}
                           for k, c in recipients.most_common(TOP_N)],
        "registry_orders": len(registry),
        "registry_revenue": round(sum(float(o.registry_revenue or 0) for o in registry), 2),
    }


def change(current: float | int | None, previous: float | int | None) -> float | None:
    """Relative change for "▲ 12% vs previous period"; None when there's no base."""
    if current is None or not previous:
        return None
    return round((current - previous) / previous, 3)
