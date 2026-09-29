"""Storefront API, reached only through the App Proxy
(/apps/giftsense/* on the shop's domain → /api/storefront/*).

    GET  /api/storefront/config   widget bootstrap (intake options, branding)
    POST /api/storefront/search   metered gift search (app/services/metering.py)

Trust: Shopify's proxy signature (app/services/proxy_auth.py); the shop is
the signed `shop` param, never anything in the body. Shoppers always get
picks: limits and AI failures only turn reasons into templates, and nothing
about plans, limits or models is ever returned to the storefront.
Rate limits per session/IP: LAUNCH_TODO week 3 (Redis).
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PLANS
from app.llm import chat
from app.plan_guard import may_generate
from app.services import metering
from app.services.gifting.brief import GiftBrief, intake_options
from app.services.gifting.embeddings import OpenAIEmbedder
from app.services.gifting.retrieval import Intake
from app.services.proxy_auth import verify_proxy_signature
from core.db.models import Shop
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


@router.post("/search")
async def gift_search(body: SearchRequest, shop: Shop = Depends(storefront_shop),
                      db: AsyncSession = Depends(get_db)):
    if not may_generate(shop):
        raise HTTPException(403, "Gift finder is not available.")
    intake = Intake(**body.model_dump(exclude={"sid"}))
    result = await metering.run_gift_search(db, shop, intake, OpenAIEmbedder(), chat_fn=chat)
    return _no_store({
        "picks": [{
            "product_id": p.product.product_id, "title": p.product.title, "url": p.product.url,
            "image_url": p.product.image_url, "price_min": p.product.price_min, "price_max": p.product.price_max,
            "reason": p.reason,
        } for p in result.recommendation.picks],
    })
