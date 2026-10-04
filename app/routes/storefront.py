"""Storefront API, reached only through the App Proxy
(/apps/giftsense/* on the shop's domain → /api/storefront/*).

    GET  /api/storefront/config   widget bootstrap (intake options, branding)
    POST /api/storefront/search   gift search: phase "instant" (no AI, free) and
                                  phase "ai" (metered, app/services/metering.py)
    POST /api/storefront/note/draft  metered AI gift-note draft (3 rewrites per gift)
    POST /api/storefront/events      batched widget events (analytics)
    POST /api/storefront/media/upload-url  presigned R2 PUT for a voice/video message
    POST /api/storefront/media/confirm     check the upload landed; returns its token
    GET  /api/storefront/m/{view_token}    the recipient's page (QR on the gift card)

Trust: Shopify's proxy signature (app/services/proxy_auth.py); the shop is
the signed `shop` param, never anything in the body. Shoppers always get
picks: limits and AI failures only turn reasons into templates, and nothing
about plans, limits or models is ever returned to the storefront.
Abuse limits: 10 searches/hour per widget session and 30 per shopper-IP hash
(app/services/rate_limit.py, 429), plus metering's per-shop hourly cap
(template picks, never an error).
"""
import html
import re
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_models import model_for_shop
from app.config import PLANS
from app.llm import chat, moderate
from app.plan_guard import may_generate
from app.services import catalog_index, delivery, media, metering, rate_limit, registry, wrap
from app.services.gift_settings import NOTE_TONES, Tone, note_settings
from app.services.gifting import vocab
from app.services.gifting.brief import GiftBrief, intake_options
from app.services.gifting.embeddings import OpenAIEmbedder
from app.services.gifting.retrieval import Intake
from app.services.proxy_auth import verify_proxy_signature
from core.db.models import CatalogProductRow, GiftEvent, GiftSession, Shop
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


def _delivery_window(shop: Shop) -> dict | None:
    settings = delivery.delivery_settings(shop)
    return delivery.date_window(settings, shop.store_timezone) if settings["enabled"] else None


@router.get("/config")
async def widget_config(shop: Shop = Depends(storefront_shop)):
    if not may_generate(shop):
        return _no_store({"enabled": False})
    features = PLANS.get(shop.plan_tier, PLANS["starter"])["features"]
    notes = note_settings(shop)
    return _no_store({
        "enabled": True,
        "show_badge": "hide_branding" not in features,
        "intake": intake_options(),
        # Banned words stay server-side; the draft endpoint enforces them.
        "notes": {"tone": notes["tone"], "max_chars": notes["max_chars"],
                  "tones": [{"value": k, "label": v} for k, v in NOTE_TONES.items()]},
        # Offered in the gift panel, never pre-selected, price always shown.
        "wrap": wrap.storefront_styles(shop) if "gift_wrap" in features else [],
        # Arrive-by date picker (Growth+): the pickable range in store time.
        "delivery": _delivery_window(shop) if "arrive_by" in features else None,
        # Voice (Growth+) / video (Pro) messages; empty until R2 is configured.
        "media": {"kinds": media.enabled_kinds(shop),
                  "max_secs": {k: v[1] for k, v in media.KINDS.items()}},
        # "Add to registry" on product pages (Pro).
        "registry": registry.available(shop),
    })


class SearchRequest(GiftBrief):
    sid: uuid.UUID                       # widget session id (localStorage)
    exclude_ids: list[str] = Field(default_factory=list, max_length=MAX_EXCLUDE_IDS)
    # The widget sends both at once: "instant" = ranked picks with template
    # reasons in ~0.5 s (no AI, nothing charged); "ai" = the metered search
    # whose picks and reasons replace them when ready.
    phase: Literal["instant", "ai"] = "ai"
    # A "Not quite right?" follow-up search (max vocab.MAX_REFINES per session).
    refine: bool = False


async def _session(db: AsyncSession, shop: Shop, sid: uuid.UUID) -> GiftSession | None:
    return (await db.execute(
        select(GiftSession).where(GiftSession.shop_id == shop.id, GiftSession.sid == sid)
    )).scalar_one_or_none()


async def _record_session(db: AsyncSession, shop: Shop, body: "SearchRequest", pick_ids: list[str]) -> None:
    """Upsert the widget session's last brief and final picks (AI phase only)."""
    intake = body.model_dump(mode="json", exclude={"sid", "phase", "exclude_ids", "refine", "locale"})
    session = await _session(db, shop, body.sid)
    if session is None:
        session = GiftSession(shop_id=shop.id, sid=body.sid, searches=0, refines=0)
        db.add(session)
    session.intake, session.last_picks = intake, pick_ids
    session.searches = (session.searches or 0) + 1
    if body.refine:
        session.refines = (session.refines or 0) + 1
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
    if body.refine and body.phase == "ai":
        session = await _session(db, shop, body.sid)
        if session is None or session.refines >= vocab.MAX_REFINES:
            raise HTTPException(409, "No more refinements for this search.")
    intake = Intake(**body.model_dump(exclude={"sid", "phase", "refine"}))
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
            "reason": p.reason, "wrap": wrap.wrappable(p.product.tags),
        } for p in picks],
    })


# ── Gift note drafts ─────────────────────────────────────────────────────────

MAX_NOTE_DRAFTS_PER_GIFT = 4          # the first draft + 3 rewrites
NOTE_DRAFTS_PER_SID_PER_HOUR = 20
NOTE_DRAFTS_PER_IP_PER_HOUR = 60


class NoteDraftRequest(BaseModel):
    sid: uuid.UUID
    product_id: Optional[str] = Field(default=None, max_length=40)   # None = one note for the whole order
    recipient: Optional[str] = None
    occasion: Optional[str] = None
    tone: Optional[Tone] = None
    # Recipient's first name as typed by the shopper: letters, spaces, . ' - only.
    name: str = Field(default="", max_length=40, pattern=r"^[\w .'\-]*$")
    # Storefront language: the note is written in it.
    locale: Optional[str] = None

    @field_validator("locale", mode="before")
    @classmethod
    def _locale(cls, v):
        return GiftBrief._locale(v)

    @field_validator("recipient")
    @classmethod
    def _recipient(cls, v):
        if v is not None and v not in vocab.RECIPIENTS:
            raise ValueError("unknown recipient")
        return v

    @field_validator("occasion")
    @classmethod
    def _occasion(cls, v):
        if v is not None and v not in vocab.OCCASIONS:
            raise ValueError("unknown occasion")
        return v


@router.post("/note/draft")
async def note_draft(body: NoteDraftRequest, request: Request, shop: Shop = Depends(storefront_shop),
                     db: AsyncSession = Depends(get_db)):
    if not may_generate(shop):
        raise HTTPException(403, "Gift notes are not available.")
    ip = rate_limit.shopper_ip(request.headers.get("x-forwarded-for"))
    if not await rate_limit.hit(f"note:sid:{shop.id}:{body.sid}", NOTE_DRAFTS_PER_SID_PER_HOUR, rate_limit.HOUR) or (
            ip and not await rate_limit.hit(f"note:ip:{shop.id}:{rate_limit.ip_hash(ip)}",
                                            NOTE_DRAFTS_PER_IP_PER_HOUR, rate_limit.HOUR)):
        raise HTTPException(429, SLOW_DOWN)

    gift_key = body.product_id or "order"
    session = await _session(db, shop, body.sid)
    drafts = dict((session.note_drafts if session else None) or {})
    count = (drafts.get(gift_key) or {}).get("count", 0)
    if count >= MAX_NOTE_DRAFTS_PER_GIFT:
        raise HTTPException(409, "You've rewritten this note a few times already. Edit it by hand to make it yours.")

    intake = (session.intake if session else None) or {}
    settings = note_settings(shop)
    tone = body.tone or settings["tone"]
    product = None
    if body.product_id:
        product = (await db.execute(select(CatalogProductRow).where(
            CatalogProductRow.shop_id == shop.id, CatalogProductRow.product_id == body.product_id))).scalar_one_or_none()
    profile = (product.gift_profile or {}) if product else {}

    result = await metering.run_note_draft(
        db, shop, chat_fn=chat, moderate_fn=moderate,
        recipient=body.recipient or intake.get("recipient"), occasion=body.occasion or intake.get("occasion"),
        tone=tone, max_chars=settings["max_chars"], banned_words=settings["banned_words"], name=body.name.strip(),
        product_title=product.title if product else "", product_pitch=profile.get("gift_pitch", ""),
        product_facts=profile.get("facts", []), locale=body.locale,
    )

    if session is None:
        session = GiftSession(shop_id=shop.id, sid=body.sid, intake={}, last_picks=[], searches=0, refines=0)
        db.add(session)
    drafts[gift_key] = {"count": count + 1, "last": result.text}
    session.note_drafts = drafts
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()

    return _no_store({"note": result.text, "source": result.source, "tone": tone, "max_chars": settings["max_chars"],
                      "rewrites_left": MAX_NOTE_DRAFTS_PER_GIFT - count - 1})


# ── Analytics beacon ─────────────────────────────────────────────────────────

EVENT_TYPES = {"widget_open", "intake_complete", "pick_click", "pick_atc", "refine",
               "panel_open", "note_drafted", "panel_submit", "wrap_added", "message_added"}
EVENTS_PER_SID_PER_HOUR = 300


class StorefrontEvent(BaseModel):
    type: str = Field(max_length=40)
    product_id: Optional[str] = Field(default=None, max_length=40)


class EventBatch(BaseModel):
    sid: uuid.UUID
    events: list[StorefrontEvent] = Field(max_length=20)


@router.post("/events")
async def events(body: EventBatch, shop: Shop = Depends(storefront_shop), db: AsyncSession = Depends(get_db)):
    """Batched widget events for analytics (docs/TECHNICAL_PLAN.md §7.1).
    Unknown types are dropped silently so old widgets never error."""
    keep = [e for e in body.events if e.type in EVENT_TYPES]
    if keep and await rate_limit.hit(f"events:sid:{shop.id}:{body.sid}", EVENTS_PER_SID_PER_HOUR, rate_limit.HOUR):
        db.add_all([GiftEvent(shop_id=shop.id, sid=body.sid, type=e.type, product_id=e.product_id) for e in keep])
        await db.commit()
    else:
        keep = []
    return _no_store({"stored": len(keep)})


# ── Voice and video messages ────────────────────────────────────────────────

MEDIA_UPLOADS_PER_SID_PER_HOUR = 10
MEDIA_UPLOADS_PER_IP_PER_HOUR = 30


class MediaUploadRequest(BaseModel):
    sid: uuid.UUID
    kind: Literal["voice", "video"]
    mime: str = Field(max_length=100)
    size: int = Field(ge=1)
    duration_s: int = Field(ge=1)


class MediaConfirmRequest(BaseModel):
    sid: uuid.UUID
    token: str = Field(min_length=10, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")


@router.post("/media/upload-url")
async def media_upload_url(body: MediaUploadRequest, request: Request, shop: Shop = Depends(storefront_shop),
                           db: AsyncSession = Depends(get_db)):
    ip = rate_limit.shopper_ip(request.headers.get("x-forwarded-for"))
    if not await rate_limit.hit(f"media:sid:{shop.id}:{body.sid}", MEDIA_UPLOADS_PER_SID_PER_HOUR, rate_limit.HOUR) or (
            ip and not await rate_limit.hit(f"media:ip:{shop.id}:{rate_limit.ip_hash(ip)}",
                                            MEDIA_UPLOADS_PER_IP_PER_HOUR, rate_limit.HOUR)):
        raise HTTPException(429, SLOW_DOWN)
    try:
        ticket = await media.issue_upload(db, shop, body.sid, body.kind, body.mime, body.size, body.duration_s)
    except media.MediaError as e:
        raise HTTPException(422, str(e))
    return _no_store({"token": ticket.token, "upload_url": ticket.upload_url, "headers": ticket.headers})


@router.post("/media/confirm")
async def media_confirm(body: MediaConfirmRequest, shop: Shop = Depends(storefront_shop),
                        db: AsyncSession = Depends(get_db)):
    try:
        row = await media.confirm_upload(db, shop, body.sid, body.token)
    except media.MediaError as e:
        raise HTTPException(422, str(e))
    return _no_store({"token": row.token, "kind": row.kind})


# ── Recipient page (/apps/giftsense/m/{view_token}) ──────────────────────────
# Rendered by Shopify as Liquid on the store's own domain: the store's name,
# no GiftSense branding, noindex, and only the one recording. The file plays
# from a short-lived presigned R2 URL; nothing else about the order is shown.

_PAGE = """{%% layout none %%}<!doctype html>
<html lang="{{ request.locale.iso_code | default: 'en' }}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<meta name="referrer" content="no-referrer"><title>%(title)s · {{ shop.name | escape }}</title>
<style>
:root { color-scheme: light dark; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px 16px;
       font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f5f2; color: #1f2937; }
@media (prefers-color-scheme: dark) { body { background: #16181d; color: #e5e7eb; } }
main { width: 100%%; max-width: 560px; text-align: center; display: grid; gap: 16px; }
h1 { font-size: 22px; margin: 0; } p { margin: 0; opacity: .75; }
video, audio { width: 100%%; border-radius: 12px; } video { background: #000; max-height: 70vh; }
a { color: inherit; }
</style></head><body><main>
<p>{{ shop.name | escape }}</p>
%(body)s
</main></body></html>"""

_GONE_PAGE = _PAGE % {"title": "Message unavailable", "body": (
    "<h1>This message isn't available</h1><p>It may have expired. Messages are kept for a limited time "
    "after the gift arrives.</p>")}


def _liquid(page: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(page, status_code=status, media_type="application/liquid",
                        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"})


@router.get("/m/{view_token}")
async def recipient_page(view_token: str, shop: Shop = Depends(storefront_shop), db: AsyncSession = Depends(get_db)):
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,40}", view_token) or not media.configured():
        return _liquid(_GONE_PAGE, 404)
    row = await media.for_viewing(db, shop.id, view_token)
    if row is None:
        return _liquid(_GONE_PAGE, 404)
    src = html.escape(media.playback_url(row), quote=True)
    if row.kind == "video":
        player = f'<video controls playsinline preload="metadata" src="{src}"></video>'
        heading = "Someone sent you a video message"
    else:
        player = f'<audio controls preload="metadata" src="{src}"></audio>'
        heading = "Someone sent you a voice message"
    return _liquid(_PAGE % {"title": heading, "body": (
        f"<h1>{heading} 🎁</h1>{player}"
        f'<p><a href="{src}" download>Can\'t play it? Download the message</a></p>')})
