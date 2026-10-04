"""Read-only queries behind the internal admin (/admin/*), trimmed from
Prudix Commerce to GiftSense's tables. Money is in USD; "this month" is the
calendar month in UTC (merchant billing cycles differ per shop)."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_models import AI_TIERS, MODELS, SLOTS, ai_tier_for, model_label, resolve_model
from app.config import PLANS
from app.plan_guard import effective_cycle_start
from core.config import settings
from core.db.models import (
    CatalogProductRow, CatalogSync, GiftMedia, GiftOrder, Registry, Shop, SupportTicket, UsageLog,
)

PAYING = ("active",)
LIVE = ("active", "trial_active")


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def price(tier: str | None) -> float:
    return float(PLANS.get(tier or "", {}).get("price_usd", 0))


async def overview(db: AsyncSession) -> dict:
    now = datetime.now(timezone.utc)
    shops = (await db.execute(select(Shop.plan_tier, Shop.plan_status, Shop.installed_at))).all()
    by_status, by_plan = defaultdict(int), defaultdict(int)
    mrr = 0.0
    for tier, status, _ in shops:
        by_status[status] += 1
        if status in LIVE:
            by_plan[tier] += 1
        if status in PAYING:
            mrr += price(tier)
    installs_7d = sum(1 for *_, at in shops if at and (at if at.tzinfo else at.replace(tzinfo=timezone.utc)) >= now - timedelta(days=7))
    ai_cost = float((await db.execute(select(func.coalesce(func.sum(UsageLog.cost_usd), 0)).where(
        UsageLog.created_at >= month_start(now)))).scalar() or 0)
    gens_24h = int((await db.execute(select(func.coalesce(func.sum(UsageLog.generations_consumed), 0)).where(
        UsageLog.created_at >= now - timedelta(hours=24)))).scalar() or 0)
    gift_orders_30d = (await db.execute(select(func.count(GiftOrder.id)).where(
        GiftOrder.created_at >= now - timedelta(days=30)))).scalar() or 0
    profit = mrr - ai_cost - settings.monthly_infra_cost_usd
    return {
        "mrr": mrr, "ai_cost_mtd": ai_cost, "infra": settings.monthly_infra_cost_usd, "profit": profit,
        "margin_pct": (profit / mrr * 100) if mrr else None,
        "total_shops": len(shops), "by_status": dict(by_status), "by_plan": dict(by_plan),
        "installs_7d": installs_7d, "generations_24h": gens_24h, "gift_orders_30d": gift_orders_30d,
        "registries": (await db.execute(select(func.count(Registry.id)))).scalar() or 0,
        "messages": (await db.execute(select(func.count(GiftMedia.id)).where(GiftMedia.status == "linked"))).scalar() or 0,
        "open_tickets": await open_ticket_count(db),
    }


async def open_ticket_count(db: AsyncSession) -> int:
    return (await db.execute(select(func.count(SupportTicket.id)).where(SupportTicket.status != "resolved"))).scalar() or 0


async def shops_list(db: AsyncSession, search: str = "", status: str = "", tier: str = "") -> list[dict]:
    query = select(Shop).order_by(Shop.installed_at.desc()).limit(500)
    if search:
        query = query.where(Shop.shop_domain.ilike(f"%{search}%"))
    if status:
        query = query.where(Shop.plan_status == status)
    if tier:
        query = query.where(Shop.plan_tier == tier)
    shops = (await db.execute(query)).scalars().all()
    since = month_start()
    usage = {sid: (int(g or 0), float(c or 0), last) for sid, g, c, last in (await db.execute(
        select(UsageLog.shop_id, func.sum(UsageLog.generations_consumed), func.sum(UsageLog.cost_usd),
               func.max(UsageLog.created_at)).where(UsageLog.created_at >= since).group_by(UsageLog.shop_id))).all()}
    products = dict((await db.execute(select(CatalogProductRow.shop_id, func.count(CatalogProductRow.id))
                                      .group_by(CatalogProductRow.shop_id))).all())
    out = []
    for s in shops:
        gens, cost, last = usage.get(s.id, (0, 0.0, None))
        p = price(s.plan_tier) if s.plan_status in PAYING else 0
        out.append({"id": s.id, "shop_domain": s.shop_domain, "plan_tier": s.plan_tier, "plan_status": s.plan_status,
                    "generations_mtd": gens, "cost_mtd": cost, "price": p,
                    "margin_pct": ((p - cost) / p * 100) if p else None, "products": products.get(s.id, 0),
                    "installed_at": s.installed_at, "last_active": last})
    return out


async def shop_detail(db: AsyncSession, shop: Shop) -> dict:
    floor = effective_cycle_start(shop)
    q = select(func.coalesce(func.sum(UsageLog.generations_consumed), 0), func.coalesce(func.sum(UsageLog.cost_usd), 0)) \
        .where(UsageLog.shop_id == shop.id)
    if floor:
        q = q.where(UsageLog.created_at >= floor)
    gens, cost = (await db.execute(q)).one()
    by_action = (await db.execute(select(UsageLog.action_type, func.count(UsageLog.id), func.sum(UsageLog.cost_usd))
                                  .where(UsageLog.shop_id == shop.id, UsageLog.created_at >= month_start())
                                  .group_by(UsageLog.action_type))).all()
    recent = (await db.execute(select(UsageLog).where(UsageLog.shop_id == shop.id)
                               .order_by(UsageLog.created_at.desc()).limit(20))).scalars().all()
    syncs = (await db.execute(select(CatalogSync).where(CatalogSync.shop_id == shop.id)
                              .order_by(CatalogSync.started_at.desc()).limit(5))).scalars().all()
    tickets = (await db.execute(select(SupportTicket).where(SupportTicket.shop_id == shop.id)
                                .order_by(SupportTicket.created_at.desc()).limit(10))).scalars().all()
    plan = PLANS.get(shop.plan_tier, PLANS["starter"])
    pins = shop.model_pins or {}
    return {
        "generations_cycle": int(gens), "generation_limit": plan["generation_limit"], "cost_cycle": float(cost),
        "by_action": [{"action": a, "calls": n, "cost": float(c or 0)} for a, n, c in by_action],
        "recent": recent, "syncs": syncs, "tickets": tickets,
        "products": (await db.execute(select(func.count(CatalogProductRow.id)).where(
            CatalogProductRow.shop_id == shop.id))).scalar() or 0,
        "gift_orders": (await db.execute(select(func.count(GiftOrder.id)).where(GiftOrder.shop_id == shop.id))).scalar() or 0,
        "ai_tier": ai_tier_for(shop.plan_tier, shop.selected_model),
        "slots": [{"slot": slot, "pin": pins.get(slot), "resolved": resolve_model(slot, shop.id, pins),
                   "label": model_label(resolve_model(slot, shop.id, pins))} for slot in SLOTS],
        "pinnable": [m for m, spec in MODELS.items() if spec["status"] == "active"],
    }


async def financials(db: AsyncSession) -> dict:
    since = month_start()
    by_model = (await db.execute(select(UsageLog.model_used, UsageLog.action_type, func.count(UsageLog.id),
                                        func.sum(UsageLog.tokens_input), func.sum(UsageLog.tokens_output),
                                        func.sum(UsageLog.cost_usd))
                                 .where(UsageLog.created_at >= since)
                                 .group_by(UsageLog.model_used, UsageLog.action_type))).all()
    shops = await shops_list(db)
    revenue_by_plan = defaultdict(lambda: [0, 0.0])
    for s in shops:
        if s["plan_status"] in PAYING:
            revenue_by_plan[s["plan_tier"]][0] += 1
            revenue_by_plan[s["plan_tier"]][1] += s["price"]
    costliest = sorted(shops, key=lambda s: s["cost_mtd"], reverse=True)[:10]
    return {
        "rows": [{"model": m, "label": model_label(m), "action": a, "calls": n, "tokens_in": int(ti or 0),
                  "tokens_out": int(to or 0), "cost": float(c or 0)} for m, a, n, ti, to, c in by_model],
        "revenue_by_plan": {k: {"shops": v[0], "mrr": v[1]} for k, v in revenue_by_plan.items()},
        "costliest": costliest,
    }


async def models_view(db: AsyncSession) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=30)
    usage = (await db.execute(select(UsageLog.model_used, func.count(UsageLog.id), func.sum(UsageLog.cost_usd),
                                     func.avg(UsageLog.duration_ms), func.count(func.distinct(UsageLog.shop_id)))
                              .where(UsageLog.created_at >= since).group_by(UsageLog.model_used))).all()
    pinned = [(s.shop_domain, s.id, s.model_pins) for s in (await db.execute(
        select(Shop).where(Shop.model_pins.is_not(None)))).scalars().all() if s.model_pins]
    return {
        "slots": [{"slot": k, **v, "tier": next((t for t, spec in AI_TIERS.items() if spec["slot"] == k), None)}
                  for k, v in SLOTS.items()],
        "models": [{"id": m, **{k: spec.get(k) for k in ("label", "provider", "price", "status", "fallback",
                                                         "replacement", "provider_retires_on")}}
                   for m, spec in MODELS.items()],
        "usage": [{"model": m, "label": model_label(m), "calls": n, "cost": float(c or 0),
                   "avg_ms": int(ms or 0), "shops": shops} for m, n, c, ms, shops in usage],
        "pinned": pinned,
    }
