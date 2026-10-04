"""Gift registries (Pro, docs/TECHNICAL_PLAN.md §6.9).

A logged-in shopper adds products to their registry from product pages (the
App Proxy passes `logged_in_customer_id`, the only customer data we keep).
One registry per customer per shop, created on the first add. Guests open
it by `share_token` and add items to their cart with a hidden
`_giftsense_registry` = "<share_token>:<item id>" line property; orders/create
counts those lines as bought (`record_purchases`).

AI suggestions for the owner reuse the gift search (metered like one, cached
per registry per day): occasion from the registry, budget from its items'
prices, its item titles as context, and its products excluded.
"""
import secrets
import uuid
from datetime import date, datetime, timezone
from statistics import median

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PLANS
from app.plan_guard import may_generate
from app.services.gifting import vocab
from app.services.gifting.retrieval import Intake
from core.db.models import CatalogProductRow, Registry, RegistryItem, Shop

MAX_ITEMS = 100
MAX_WANTED = 20
MAX_TITLE = 80
# Registry occasions shown to the owner → the gift finder's occasion vocabulary.
OCCASIONS = {
    "wedding": "Wedding", "new_baby": "Baby", "birthday": "Birthday", "housewarming": "Housewarming",
    "graduation": "Graduation", "anniversary": "Anniversary", "holiday": "Holiday", "just_because": "Wish list",
}
RECIPIENT_FOR = {"wedding": "partner", "new_baby": "new_baby", "anniversary": "partner"}
SUGGESTIONS = 6


class RegistryError(Exception):
    """Shown to the shopper as-is."""


def available(shop: Shop) -> bool:
    return may_generate(shop) and "registries" in PLANS.get(shop.plan_tier, PLANS["starter"])["features"]


async def for_owner(db: AsyncSession, shop: Shop, customer_id: str) -> Registry | None:
    return (await db.execute(select(Registry).where(
        Registry.shop_id == shop.id, Registry.customer_id == customer_id))).scalar_one_or_none()


async def by_token(db: AsyncSession, shop: Shop, share_token: str) -> Registry | None:
    return (await db.execute(select(Registry).where(
        Registry.shop_id == shop.id, Registry.share_token == share_token))).scalar_one_or_none()


async def items_of(db: AsyncSession, registry: Registry) -> list[RegistryItem]:
    return list((await db.execute(select(RegistryItem).where(RegistryItem.registry_id == registry.id)
                                  .order_by(RegistryItem.created_at, RegistryItem.id))).scalars())


async def get_or_create(db: AsyncSession, shop: Shop, customer_id: str) -> Registry:
    registry = await for_owner(db, shop, customer_id)
    if registry is None:
        registry = Registry(shop_id=shop.id, customer_id=customer_id, share_token=secrets.token_urlsafe(12),
                            title="My registry", occasion="just_because")
        db.add(registry)
        await db.flush()
    return registry


async def add_item(db: AsyncSession, shop: Shop, customer_id: str, *, product_id: str, variant_id: str,
                   variant_title: str = "", quantity: int = 1) -> RegistryItem:
    """Add (or top up) a variant. Display fields come from our catalog, not
    the request, except the variant's name, which the catalog doesn't hold."""
    product = (await db.execute(select(CatalogProductRow).where(
        CatalogProductRow.shop_id == shop.id, CatalogProductRow.product_id == product_id))).scalar_one_or_none()
    if product is None or product.excluded:
        raise RegistryError("This product can't be added to a registry.")
    registry = await get_or_create(db, shop, customer_id)
    item = (await db.execute(select(RegistryItem).where(
        RegistryItem.registry_id == registry.id, RegistryItem.variant_id == variant_id))).scalar_one_or_none()
    if item is None:
        count = len(await items_of(db, registry))
        if count >= MAX_ITEMS:
            raise RegistryError(f"A registry can hold up to {MAX_ITEMS} items.")
        item = RegistryItem(registry_id=registry.id, shop_id=shop.id, product_id=product_id, variant_id=variant_id,
                            wanted_qty=0, bought_qty=0)
        db.add(item)
    item.title = product.title
    item.variant_title = "" if variant_title in ("", "Default Title") else variant_title[:80]
    item.image_url, item.url = product.image_url, product.url
    item.price = float(product.price_min or 0)
    item.wanted_qty = min(MAX_WANTED, (item.wanted_qty or 0) + max(1, quantity))
    await db.commit()
    return item


async def update_item(db: AsyncSession, registry: Registry, item_id: uuid.UUID, wanted_qty: int) -> None:
    """wanted_qty 0 removes the item."""
    item = (await db.execute(select(RegistryItem).where(
        RegistryItem.registry_id == registry.id, RegistryItem.id == item_id))).scalar_one_or_none()
    if item is None:
        raise RegistryError("That item isn't on your registry.")
    if wanted_qty <= 0:
        await db.delete(item)
    else:
        item.wanted_qty = min(MAX_WANTED, wanted_qty)
    await db.commit()


def update_details(registry: Registry, title: str | None, occasion: str | None, event_date: date | None) -> None:
    if title is not None:
        registry.title = " ".join(title.split())[:MAX_TITLE] or "My registry"
    if occasion is not None:
        if occasion not in OCCASIONS:
            raise RegistryError("Unknown occasion.")
        registry.occasion = occasion
        registry.suggestions = None
    registry.event_date = event_date


def parse_line(value: str | None) -> tuple[str, uuid.UUID] | None:
    """`_giftsense_registry` line property → (share_token, item id)."""
    try:
        token, item = str(value or "").split(":", 1)
        return (token, uuid.UUID(item)) if token else None
    except ValueError:
        return None


async def record_purchases(db: AsyncSession, shop_id, lines: list[tuple[str, int, float]]) -> tuple[uuid.UUID, float] | None:
    """orders/create: [(property value, quantity, line revenue)] → bought counts
    go up. Returns (registry id, revenue) for the gift_orders row."""
    found, revenue = None, 0.0
    for value, qty, line_revenue in lines:
        parsed = parse_line(value)
        if parsed is None:
            continue
        token, item_id = parsed
        item = (await db.execute(
            select(RegistryItem).join(Registry, Registry.id == RegistryItem.registry_id).where(
                Registry.shop_id == shop_id, Registry.share_token == token, RegistryItem.id == item_id,
            ))).scalar_one_or_none()
        if item is None:
            continue
        item.bought_qty = (item.bought_qty or 0) + max(1, qty)
        found = found or item.registry_id
        revenue += line_revenue
    return (found, round(revenue, 2)) if found else None


def suggestion_intake(registry: Registry, items: list[RegistryItem]) -> Intake:
    prices = [float(i.price) for i in items if i.price]
    mid = median(prices) if prices else 50
    band = next((b for b, (lo, hi) in vocab.BUDGET_BANDS.items() if mid >= lo and (hi is None or mid < hi)), "50_100")
    occasion = registry.occasion if registry.occasion in vocab.OCCASIONS else "just_because"
    return Intake(
        recipient=RECIPIENT_FOR.get(occasion, "anyone"), occasion=occasion, budget_band=band,
        free_text=("Registry with: " + ", ".join(i.title for i in items[:8]))[:300] if items else "",
        exclude_ids=[i.product_id for i in items][:50],
    )


def cached_suggestions(registry: Registry, today: date | None = None) -> list[dict] | None:
    s = registry.suggestions or {}
    return s.get("picks") if s.get("day") == (today or datetime.now(timezone.utc).date()).isoformat() else None


async def delete_for_customer(db: AsyncSession, shop_id, customer_id: str) -> int:
    ids = list((await db.execute(select(Registry.id).where(
        Registry.shop_id == shop_id, Registry.customer_id == customer_id))).scalars())
    if ids:
        await db.execute(delete(RegistryItem).where(RegistryItem.registry_id.in_(ids)))
        await db.execute(delete(Registry).where(Registry.id.in_(ids)))
    return len(ids)
