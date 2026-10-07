"""Internal admin (at ADMIN_PATH, default /admin), signed-in session (app/admin/auth.py, login.py). Trimmed
from Prudix Commerce to GiftSense: Overview, Money, Shops (+ per-shop AI
model pins), AI models (slots, rollout, usage by model), Support, System.

POSTs check that Origin/Referer is this host: browsers resend cached Basic
Auth credentials on cross-site form posts, so this stops CSRF.
"""
import asyncio
import os
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import queries as q
from app.admin.auth import admin_path, require_admin
from app.ai_models import MODELS, SLOTS
from app.services import media
from core.config import settings
from core.db.models import CatalogSync, Shop, SupportTicket
from core.db.session import get_db

# Mounted at ADMIN_PATH in app/main.py (prefix is env-configurable).
router = APIRouter(tags=["admin"], dependencies=[Depends(require_admin)])
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


class _AdminPath:
    """`{{ admin_path }}` in templates, resolved at render time."""
    def __str__(self) -> str:
        return admin_path()


templates.env.globals["admin_path"] = _AdminPath()
STATUSES = ("open", "in_progress", "resolved")


def _sentry_url() -> str | None:
    if settings.sentry_project_url:
        return settings.sentry_project_url
    project = settings.sentry_dsn.rstrip("/").rsplit("/", 1)[-1] if settings.sentry_dsn else ""
    return f"https://sentry.io/issues/?project={project}" if project.isdigit() else None


async def _ctx(request: Request, db: AsyncSession, active: str, **kw) -> dict:
    return {"request": request, "active": active, "env": settings.app_env, "sentry_project_url": _sentry_url(),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "open_count": await q.open_ticket_count(db), **kw}


def same_origin(request: Request) -> None:
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    if urlparse(source).netloc != request.url.netloc:
        raise HTTPException(403, "Cross-site request refused.")


def _page(request, name, ctx):
    return templates.TemplateResponse(request, name, ctx, headers={"Cache-Control": "no-store"})


@router.get("/", response_class=HTMLResponse)
async def overview(request: Request, db: AsyncSession = Depends(get_db)):
    return _page(request, "overview.html", await _ctx(request, db, "overview", k=await q.overview(db)))


@router.get("/financials", response_class=HTMLResponse)
async def financials(request: Request, db: AsyncSession = Depends(get_db)):
    return _page(request, "financials.html", await _ctx(request, db, "financials", f=await q.financials(db),
                                                        k=await q.overview(db)))


@router.get("/shops", response_class=HTMLResponse)
async def shops(request: Request, search: str = "", status: str = "", tier: str = "",
                db: AsyncSession = Depends(get_db)):
    return _page(request, "shops.html", await _ctx(
        request, db, "shops", shops=await q.shops_list(db, search.strip(), status, tier),
        search=search, status_filter=status, tier_filter=tier))


async def _shop(db: AsyncSession, shop_id: uuid.UUID) -> Shop:
    shop = (await db.execute(select(Shop).where(Shop.id == shop_id))).scalar_one_or_none()
    if shop is None:
        raise HTTPException(404, "Shop not found")
    return shop


@router.get("/shops/{shop_id}", response_class=HTMLResponse)
async def shop_detail(shop_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    shop = await _shop(db, shop_id)
    return _page(request, "shop_detail.html", await _ctx(request, db, "shops", shop=shop,
                                                         d=await q.shop_detail(db, shop)))


@router.post("/shops/{shop_id}/pins")
async def save_pins(shop_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    """Pin a shop's slot to a model (e.g. hold it on the old model during a
    rollout, or try a new one on one store). Empty = follow the slot."""
    same_origin(request)
    shop = await _shop(db, shop_id)
    form = await request.form()
    pins = {}
    for slot in SLOTS:
        model = (form.get(slot) or "").strip()
        if model:
            if MODELS.get(model, {}).get("status") != "active":
                raise HTTPException(422, f"{model} isn't an active model")
            pins[slot] = model
    shop.model_pins = pins or None
    await db.commit()
    return RedirectResponse(f"{admin_path()}/shops/{shop_id}?saved=1", status_code=303)


@router.get("/models", response_class=HTMLResponse)
async def models(request: Request, db: AsyncSession = Depends(get_db)):
    return _page(request, "models.html", await _ctx(request, db, "models", m=await q.models_view(db)))


@router.get("/support", response_class=HTMLResponse)
async def support(request: Request, status: str = "", db: AsyncSession = Depends(get_db)):
    query = select(SupportTicket, Shop.shop_domain).join(Shop, Shop.id == SupportTicket.shop_id) \
        .order_by(SupportTicket.created_at.desc()).limit(200)
    if status in STATUSES:
        query = query.where(SupportTicket.status == status)
    rows = (await db.execute(query)).all()
    return _page(request, "support.html", await _ctx(request, db, "support", tickets=rows, status_filter=status,
                                                     statuses=STATUSES))


@router.post("/support/{ticket_id}/update")
async def support_update(ticket_id: uuid.UUID, request: Request, status: str = Form(...),
                         admin_notes: str = Form(""), db: AsyncSession = Depends(get_db)):
    same_origin(request)
    if status not in STATUSES:
        raise HTTPException(422, "Unknown status")
    ticket = (await db.execute(select(SupportTicket).where(SupportTicket.id == ticket_id))).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(404, "Ticket not found")
    ticket.status, ticket.admin_notes = status, admin_notes.strip()[:5000] or None
    ticket.resolved_at = datetime.now(timezone.utc) if status == "resolved" else None
    await db.commit()
    return RedirectResponse(f"{admin_path()}/support", status_code=303)


HEALTH_KEY = "arq:queue:health-check"
QUEUE_KEY = "arq:queue"


def _beat(raw: str) -> str:
    """ARQ heartbeat "Oct-04 10:59:12 j_complete=214 j_failed=0 …" → readable."""
    import re
    stats = dict(re.findall(r"(\w+)=(\d+)", raw))
    when = raw.split(" j_")[0].split(" ")[-1]
    return (f"Last heartbeat {when} · {stats.get('j_complete', '0')} jobs done · {stats.get('j_failed', '0')} failed"
            f" · {stats.get('j_ongoing', '0')} running")


async def _health() -> dict:
    """Database and Redis ping times, and the ARQ worker's heartbeat + queue."""
    import time
    from sqlalchemy import text
    from core.db.session import engine
    out = {"services": [], "worker": None}

    async def probe(name, desc, fn):
        started = time.perf_counter()
        try:
            async with asyncio.timeout(2):
                detail = await fn()
            out["services"].append({"name": name, "desc": desc, "ok": True,
                                    "ms": round((time.perf_counter() - started) * 1000), "detail": detail})
        except Exception as e:  # noqa: BLE001
            out["services"].append({"name": name, "desc": desc, "ok": False, "ms": None, "detail": str(e)[:160]})

    async def db():
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def redis():
        import redis.asyncio as redis_asyncio
        client = redis_asyncio.from_url(settings.redis_url)
        try:
            await client.ping()
            beat = await client.get(HEALTH_KEY)
            queued = await client.zcard(QUEUE_KEY)
            out["worker"] = {"alive": beat is not None, "beat": _beat(beat.decode()) if beat else None, "queued": queued}
        finally:
            await client.close()

    await probe("Database", "Postgres (Supabase)", db)
    await probe("Redis", "Queue, rate limits, locks", redis)
    return out


@router.get("/system", response_class=HTMLResponse)
async def system(request: Request, db: AsyncSession = Depends(get_db)):
    # (label, ok, what breaks when it's not, badge when ok, badge when not)
    def item(label, ok, impact, yes="Set", no="Missing"):
        return label, ok, impact, yes, no
    groups = [
        ("AI", [item("OpenAI key", bool(settings.openai_api_key), "Gift search, notes and catalog analysis stop"),
                item("Anthropic key", bool(settings.anthropic_api_key), "The Premium AI option fails over to GPT")]),
        ("Storage and email", [
            item("Cloudflare R2", media.configured(), "Voice and video messages are hidden"),
            item("Postmark", bool(settings.postmark_server_token), "Emails are only logged, not sent"),
            item("Shopify app handle", bool(settings.shopify_app_handle), "Email links use the client id instead")]),
        ("Operations", [
            item("Sentry", bool(settings.sentry_dsn), "Errors aren't reported"),
            item("Billing", not settings.billing_test_mode, "Charges are test charges: fine in dev, must be live at launch",
                 yes="Live", no="Test mode")]),
    ]
    running = (await db.execute(select(CatalogSync, Shop.shop_domain).join(Shop, Shop.id == CatalogSync.shop_id)
                                .where(CatalogSync.status.in_(("queued", "running")))
                                .order_by(CatalogSync.started_at.desc()).limit(20))).all()
    wrap_errors = [(s.shop_domain, ((s.gift_settings or {}).get("wrap") or {}).get("error"))
                   for s in (await db.execute(select(Shop).where(Shop.gift_settings.is_not(None)))).scalars()
                   if ((s.gift_settings or {}).get("wrap") or {}).get("error")]
    stuck = [r for r in running if r[0].started_at and
             (datetime.now(timezone.utc) - (r[0].started_at if r[0].started_at.tzinfo else
                                            r[0].started_at.replace(tzinfo=timezone.utc))).total_seconds() > 3600]
    return _page(request, "system.html", await _ctx(
        request, db, "system", health=await _health(), groups=groups, running=running, stuck=len(stuck),
        wrap_errors=wrap_errors, env_info=[("Environment", settings.app_env), ("App host", settings.get_app_host() or "—"),
                                           ("Shopify API version", settings.shopify_api_version),
                                           ("App handle", settings.shopify_app_handle or "— (links use the client id)")]))
