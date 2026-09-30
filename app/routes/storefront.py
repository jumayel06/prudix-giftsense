"""Storefront API, reached only through the App Proxy
(/apps/giftsense/* on the shop's domain → /api/storefront/*).

    GET  /api/storefront/config   widget bootstrap (intake options, branding)
    POST /api/storefront/search   gift search: phase "instant" (no AI, free) and
                                  phase "ai" (metered, app/services/metering.py)

Trust: Shopify's proxy signature (app/services/proxy_auth.py); the shop is
the signed `shop` param, never anything in the body. Shoppers always get
picks: limits and AI failures only turn reasons into templates, and nothing
about plans, limits or models is ever returned to the storefront.
Abuse limits: 10 searches/hour per widget session and 30 per shopper-IP hash
(app/services/rate_limit.py, 429), plus metering's per-shop hourly cap
(template picks, never an error).
"""
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PLANS
from app.llm import chat
from app.plan_guard import may_generate
from app.ai_models import model_for_shop
from app.services import catalog_index, metering, rate_limit
from app.services.gifting.brief import GiftBrief, intake_options
from app.services.gifting.embeddings import OpenAIEmbedder
from app.services.gifting.retrieval import Intake
from app.services.proxy_auth import verify_proxy_signature
from core.db.models import GiftSession, Shop
from core.db.session import get_db

router = APIRouter(prefix="/api/storefront")

MAX_EXCLUDE_IDS = 50
_GONE = {"uninstalled", "purged"}


async def storefront_shop(request: Request, db: AsyncSession = Depends(get_db)) -> Shop:
    if not verify_proxy_signature(request.query_params):
        raise HTTPException(401, "Invalid proxy signature.")
    domain = request.query_params.get("shop", "")
    shop = (await db.execute(select(Shop).where(Shop.shop_domain == domain))).scalar_one_or_none()
    if shop is None or shop.plan_status in _GONE:
        raise HTTPException(404, "Shop not found.")
    return shop


def _no_store(payload: dict) -> JSONResponse:
    # Per-shopper answers: never cache at Shopify's edge or in the browser.
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.get("/config")
async def widget_config(shop: Shop = Depends(storefront_shop)):
    if not may_generate(shop):
        return _no_store({"enabled": False})
    features = PLANS.get(shop.plan_tier, PLANS["starter"])["features"]
    return _no_store({
        "enabled": True,
        "show_badge": "hide_branding" not in features,
        "intake": intake_options(),
    })


class SearchRequest(GiftBrief):
    sid: uuid.UUID                       # widget session id (localStorage)
    exclude_ids: list[str] = Field(default_factory=list, max_length=MAX_EXCLUDE_IDS)
    # The widget sends both at once: "instant" = ranked picks with template
    # reasons in ~0.5 s (no AI, nothing charged); "ai" = the metered search
    # whose picks and reasons replace them when ready.
    phase: Literal["instant", "ai"] = "ai"


async def _record_session(db: AsyncSession, shop: Shop, body: "SearchRequest", pick_ids: list[str]) -> None:
    """Upsert the widget session's last brief and final picks (AI phase only)."""
    intake = body.model_dump(mode="json", exclude={"sid", "phase", "exclude_ids"})
    session = (await db.execute(
        select(GiftSession).where(GiftSession.shop_id == shop.id, GiftSession.sid == body.sid)
    )).scalar_one_or_none()
    if session is None:
        session = GiftSession(shop_id=shop.id, sid=body.sid, searches=0)
        db.add(session)
    session.intake, session.last_picks = intake, pick_ids
    session.searches = (session.searches or 0) + 1
    try:
        await db.commit()
    except IntegrityError:   # the same sid's first two searches raced; the other one won
        await db.rollback()


SLOW_DOWN = "You've searched a lot in a short time. Please try again in a little while."


async def _within_abuse_limits(request: Request, shop: Shop, sid: uuid.UUID, phase: str) -> bool:
    # Separate counters per phase, so one search (instant + ai) counts once in each.
    if not await rate_limit.hit(f"{phase}:sid:{shop.id}:{sid}", rate_limit.SEARCHES_PER_SID_PER_HOUR, rate_limit.HOUR):
        return False
    ip = rate_limit.shopper_ip(request.headers.get("x-forwarded-for"))
    if ip and not await rate_limit.hit(f"{phase}:ip:{shop.id}:{rate_limit.ip_hash(ip)}",
                                       rate_limit.SEARCHES_PER_IP_PER_HOUR, rate_limit.HOUR):
        return False
    return True


@router.post("/search")
async def gift_search(body: SearchRequest, request: Request, shop: Shop = Depends(storefront_shop),
                      db: AsyncSession = Depends(get_db)):
    if not may_generate(shop):
        raise HTTPException(403, "Gift finder is not available.")
    if not await _within_abuse_limits(request, shop, body.sid, body.phase):
        raise HTTPException(429, SLOW_DOWN)
    intake = Intake(**body.model_dump(exclude={"sid", "phase"}))
    if body.phase == "instant":
        rec, _ = await catalog_index.recommend_for_shop(db, shop.id, intake, OpenAIEmbedder(),
                                                        model_for_shop(shop), use_llm=False)
        picks = rec.picks
    else:
        picks = (await metering.run_gift_search(db, shop, intake, OpenAIEmbedder(), chat_fn=chat)).recommendation.picks
        await _record_session(db, shop, body, [p.product.product_id for p in picks])
    return _no_store({
        "phase": body.phase,
        "picks": [{
            "product_id": p.product.product_id, "title": p.product.title, "url": p.product.url,
            "image_url": p.product.image_url, "price_min": p.product.price_min, "price_max": p.product.price_max,
            "reason": p.reason,
        } for p in picks],
    })
