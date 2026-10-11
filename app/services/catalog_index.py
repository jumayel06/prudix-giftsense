"""Gift search over a shop's stored catalog (docs/TECHNICAL_PLAN.md §4.4).

    ≤ SMALL_CATALOG_MAX eligible products → load them all, no embedding call
    larger catalogs → embed the brief once; Postgres filters (stock, excluded,
        budget) and orders by cosine distance (`<=>`), then the in-memory
        pipeline scores, diversifies and reranks the top PREFETCH rows.

An exact scan, no HNSW index: per-shop catalogs are ≤ 5,000 rows, so it takes
milliseconds and avoids filtered-ANN recall problems. SQLite (tests) has no
`<=>`, so there the prefetch is unordered and ranking happens in Python.
"""
import time

from sqlalchemy import Float, bindparam, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import chat
from app.services.catalog_sync import row_to_product
from app.services.gifting import vocab
from app.services.gifting.embeddings import EMBEDDING_DIMS, Embedder
from app.services.gifting.pipeline import SMALL_CATALOG_MAX, Recommendation, recommend
from app.services.gifting.profile import GiftProfile, apply_overrides
from app.services.gifting.rerank import ChatFn
from app.services.gifting.retrieval import (BUDGET_FLOOR_SLACK, MIN_GIFTABLE, IndexedProduct, Intake,
                                            price_in_budget, query_text)
from core.db.models import CatalogProductRow
from core.db.types import Embedding

PREFETCH = 200
NOTE_PREFETCH = 50


def _eligible(shop_id):
    return [
        CatalogProductRow.shop_id == shop_id,
        CatalogProductRow.available.is_(True),
        CatalogProductRow.excluded.is_(False),
        CatalogProductRow.gift_profile.is_not(None),
        CatalogProductRow.embedding.is_not(None),
    ]


def _in_budget(budget_band: str):
    lo, hi = vocab.budget_range(budget_band)
    conds = [CatalogProductRow.price_max >= lo * BUDGET_FLOOR_SLACK]
    if hi is not None:
        conds.append(CatalogProductRow.price_min <= hi)
    return conds


def to_indexed(row: CatalogProductRow) -> IndexedProduct:
    profile = apply_overrides(GiftProfile(**row.gift_profile), row.merchant_overrides)
    return IndexedProduct(row_to_product(row), profile, list(row.embedding or []))


async def count_eligible(db: AsyncSession, shop_id) -> int:
    return (await db.execute(
        select(func.count()).select_from(CatalogProductRow).where(*_eligible(shop_id))
    )).scalar_one()


async def load_items(
    db: AsyncSession, shop_id, intake: Intake, query_vector: list[float] | None = None,
    note_vector: list[float] | None = None,
) -> list[IndexedProduct]:
    base = select(CatalogProductRow).where(*_eligible(shop_id), *_in_budget(intake.budget_band))
    if query_vector is None or db.bind.dialect.name != "postgresql":
        return [to_indexed(r) for r in (await db.execute(base)).scalars().all()]
    rows: dict = {}
    # Nearest to the whole brief, plus nearest to the shopper's note on its own
    # (so "a book" in a 5,000-product store still reaches the shortlist).
    for vec, limit in ((query_vector, PREFETCH), (note_vector, NOTE_PREFETCH)):
        if vec is None:
            continue
        v = bindparam("v", vec, type_=Embedding(EMBEDDING_DIMS))
        q = base.order_by(CatalogProductRow.embedding.op("<=>", return_type=Float)(v)).limit(limit)
        for r in (await db.execute(q)).scalars().all():
            rows.setdefault(r.id, r)
    return [to_indexed(r) for r in rows.values()]


async def recommend_for_shop(
    db: AsyncSession,
    shop_id,
    intake: Intake,
    embedder: Embedder,
    model: str,
    chat_fn: ChatFn = chat,
    use_llm: bool = True,
) -> tuple[Recommendation, int]:
    """Returns (recommendation, latency_ms)."""
    started = time.monotonic()
    if await count_eligible(db, shop_id) <= SMALL_CATALOG_MAX:
        items = await load_items(db, shop_id, intake)
        rec = await recommend(intake, items, embedder, model, chat_fn=chat_fn, use_llm=use_llm)
    else:
        note = intake.free_text.strip()
        vecs = await embedder.embed([query_text(intake)] + ([note] if note else []))
        qvec, nvec = vecs[0], (vecs[1] if note else None)
        items = await load_items(db, shop_id, intake, qvec, nvec)
        rec = await recommend(intake, items, embedder, model, chat_fn=chat_fn, use_llm=use_llm,
                              query_vector=qvec, note_vector=nvec)
    return rec, int((time.monotonic() - started) * 1000)


# ── Budget choices the store can fill ────────────────────────────────────────
# Budget is the shopper answer that filters (retrieval.in_budget), so a band
# with nothing in it ends the finder with no ideas. The widget only offers
# bands with enough products; the bands themselves stay fixed (analytics,
# reasons and translations use them). Counted from live rows (price, stock,
# exclusions), not at sync time, and cached a few minutes per shop.
MIN_PRODUCTS_PER_BUDGET = 3
_BUDGET_CACHE_SECS = 300
_budget_cache: dict = {}


async def available_budget_bands(db: AsyncSession, shop_id) -> list[str]:
    hit = _budget_cache.get(shop_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    rows = (await db.execute(
        select(CatalogProductRow.price_min, CatalogProductRow.price_max,
               CatalogProductRow.gift_profile["giftable"].as_float())
        .where(CatalogProductRow.shop_id == shop_id, CatalogProductRow.available.is_(True),
               CatalogProductRow.excluded.is_(False), CatalogProductRow.embedding.is_not(None))
    )).all()
    counts = {band: sum(1 for lo, hi, giftable in rows
                        if (giftable is None or giftable >= MIN_GIFTABLE) and price_in_budget(float(lo), hi and float(hi), band))
              for band in vocab.BUDGET_BANDS}
    bands = ([b for b, n in counts.items() if n >= MIN_PRODUCTS_PER_BUDGET]
             or [b for b, n in counts.items() if n] or list(vocab.BUDGET_BANDS))
    _budget_cache[shop_id] = (time.monotonic() + _BUDGET_CACHE_SECS, bands)
    return bands
