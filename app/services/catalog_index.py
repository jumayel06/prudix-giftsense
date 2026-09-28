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
from app.services.gifting.retrieval import BUDGET_FLOOR_SLACK, IndexedProduct, Intake, query_text
from core.db.models import CatalogProductRow
from core.db.types import Embedding

PREFETCH = 200


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
) -> list[IndexedProduct]:
    q = select(CatalogProductRow).where(*_eligible(shop_id), *_in_budget(intake.budget_band))
    if query_vector is not None and db.bind.dialect.name == "postgresql":
        qvec = bindparam("qvec", query_vector, type_=Embedding(EMBEDDING_DIMS))
        q = q.order_by(CatalogProductRow.embedding.op("<=>", return_type=Float)(qvec)).limit(PREFETCH)
    rows = (await db.execute(q)).scalars().all()
    return [to_indexed(r) for r in rows]


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
        [qvec] = await embedder.embed([query_text(intake)])
        items = await load_items(db, shop_id, intake, qvec)
        rec = await recommend(intake, items, embedder, model, chat_fn=chat_fn, use_llm=use_llm, query_vector=qvec)
    return rec, int((time.monotonic() - started) * 1000)
