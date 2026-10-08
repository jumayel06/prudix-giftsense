"""GET /api/analytics?days=7|30|90: the dashboard's Analytics page.

Basic analytics (all plans): the last 30 days of BASIC_FIELDS: gift finder
use, gift orders and their sales (app/services/analytics.py). The dashboard
shows the rest of the page locked.
Full analytics (Growth+): 7, 30 or 90 days, every rate and breakdown, and the
same numbers for the period before, so the page shows what changed.

GET /api/analytics/generations: paginated log of individual AI generations
(feature, model, status), ported from Prudix Commerce — see that function's
docstring for the reservation/settle/refund row shapes it reads.
"""
from typing import Annotated
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_models import model_label
from app.config import PLANS
from app.services import analytics as svc
from core.db.models import Shop, UsageLog
from core.db.session import get_db
from core.shopify_deps import get_current_shop

router = APIRouter()

DAYS = 30
RANGES = (7, 30, 90)
BASIC_FIELDS = ("days", "currency", "sessions", "gift_orders", "attributed_orders", "attributed_revenue",
                "gift_revenue", "daily")
COMPARED = ("sessions", "gift_orders", "attributed_revenue", "conversion_rate", "gift_revenue", "all_orders")

# Mirror of the action_type values UsageLog actually gets for merchant-facing
# AI use (app/services/metering.py reserve(), app/services/registry.py). Not
# catalog_analysis — that's always 0 generations (our cost, not the
# merchant's), so it never shows up in a generations_consumed-based log.
USAGE_ACTION_LABELS = {
    "gift_search":      "Gift finder searches",
    "playground":       "Try it (dashboard)",
    "gift_note":        "AI gift notes",
    "registry_suggest": "Registry AI ideas",
}


@router.get("/api/analytics")
async def analytics(days: int = Query(DAYS), shop: Shop = Depends(get_current_shop),
                    db: AsyncSession = Depends(get_db)):
    if days not in RANGES:
        raise HTTPException(422, f"days must be one of {RANGES}")
    full = "full_analytics" in PLANS.get(shop.plan_tier, PLANS["starter"])["features"]
    days = days if full else DAYS
    now = datetime.now(timezone.utc)
    data = await svc.summary(db, shop, days, now, full=full)
    if not full:
        return {**{k: data[k] for k in BASIC_FIELDS}, "full": False}
    data["full"] = full
    if full:
        before = await svc.summary(db, shop, days, now - timedelta(days=days), daily=False)
        data["previous"] = {k: before[k] for k in COMPARED}
        data["changes"] = {k: svc.change(data[k], before[k]) for k in COMPARED}
    return data


def _parse_filter_date(raw: str, *, end: bool):
    """Parse a filter bound into an aware datetime. Bare 'YYYY-MM-DD' → a UTC
    day (the `end` bound advances to the next midnight, inclusive of that
    day). Raises HTTPException(400) on a malformed value."""
    try:
        d = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid date '{raw}' — expected YYYY-MM-DD")
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
        if end:
            d += timedelta(days=1)
    return d


@router.get("/api/analytics/generations")
async def generation_log(
    shop: Shop = Depends(get_current_shop),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    start: Annotated[str | None, Query(description="Inclusive start date YYYY-MM-DD")] = None,
    end: Annotated[str | None, Query(description="Inclusive end date YYYY-MM-DD")] = None,
    feature: Annotated[str | None, Query(description="Feature label to filter by")] = None,
    model: Annotated[str | None, Query(description="Model id to filter by")] = None,
):
    """Paginated log of individual generations, newest first — powers the
    Analytics "Generation history" table.

    UsageLog is append-only with a reserve→settle shape per generation
    (app/services/metering.py): a reservation row (generations_consumed > 0)
    written before the AI call, a completion row (== 0, tokens/cost only) on
    success, or a refund row (< 0) on failure. One line per generation here =
    the reservation row; completion rows are admin-only and dropped (else
    every generation would show a phantom "0" line). A refund row flips its
    paired reservation to "Refunded", paired per action_type in newest-first
    order (ported from Prudix Commerce, which hit the same shape).

    Filters (all optional): `start`/`end` date range (defaults to the last 90
    days when neither is given), `feature` (label), `model` (id).
    """
    now = datetime.now(timezone.utc)
    start_dt = _parse_filter_date(start, end=False) if start else None
    end_dt = _parse_filter_date(end, end=True) if end else None
    if start_dt is None and end_dt is None:
        start_dt = now - timedelta(days=90)

    conds = [UsageLog.shop_id == shop.id]
    if start_dt is not None:
        conds.append(UsageLog.created_at >= start_dt)
    if end_dt is not None:
        conds.append(UsageLog.created_at < end_dt)
    if model:
        conds.append(UsageLog.model_used == model)
    if feature:
        action_types = [k for k, v in USAGE_ACTION_LABELS.items() if v == feature]
        if action_types:
            conds.append(UsageLog.action_type.in_(action_types))

    WINDOW = 1000
    rows = (await db.execute(
        select(UsageLog).where(*conds).order_by(UsageLog.created_at.desc()).limit(WINDOW)
    )).scalars().all()

    pending_refunds: dict[str, int] = {}
    charges: list[tuple] = []  # (row, refunded)
    for r in rows:  # newest-first
        g = r.generations_consumed or 0
        if g < 0:
            pending_refunds[r.action_type] = pending_refunds.get(r.action_type, 0) + 1
        elif g > 0:
            refunded = pending_refunds.get(r.action_type, 0) > 0
            if refunded:
                pending_refunds[r.action_type] -= 1
            charges.append((r, refunded))
        # g == 0 → completion supplement (admin-only tokens/cost): skip.

    facet_rows = (await db.execute(
        select(UsageLog.action_type, UsageLog.model_used)
        .where(UsageLog.shop_id == shop.id, UsageLog.generations_consumed > 0)
        .distinct()
    )).all()
    feature_options = sorted({USAGE_ACTION_LABELS.get(a, a) for a, _ in facet_rows if a})
    model_options = sorted(
        ({"value": m, "label": model_label(m)} for m in {m for _, m in facet_rows if m}),
        key=lambda o: o["label"],
    )

    page = charges[offset:offset + limit]
    return {
        # Tokens + cost are intentionally NOT returned here — admin-only.
        "generations": [
            {
                "id":      str(r.id),
                "when":    r.created_at.isoformat() if r.created_at else None,
                "feature": USAGE_ACTION_LABELS.get(r.action_type, r.action_type),
                "model":   model_label(r.model_used) if r.model_used else None,
                "gens":    r.generations_consumed,
                "status":  "Refunded" if refunded else "Completed",
            }
            for (r, refunded) in page
        ],
        "total":         len(charges),
        "has_more":      offset + limit < len(charges),
        "window_capped": len(rows) >= WINDOW,
        "facets":        {"features": feature_options, "models": model_options},
    }
