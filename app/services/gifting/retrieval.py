"""Retrieval and ranking, no LLM (docs/TECHNICAL_PLAN.md §4.4).

1. Hard filters: available, not excluded, plausibly giftable, age band, and
   price within budget. Budget violations must be zero (eval invariant).
2. Hybrid score: embedding similarity + exact vocabulary overlap + giftable.
3. MMR diversity so the shortlist isn't a wall of near-identical items.

Pure functions over in-memory candidates. In production the similarity scan
runs in Postgres (pgvector, per shop); this module ranks what it returns.
"""
from dataclasses import dataclass, field

from app.services.gifting import vocab
from app.services.gifting.catalog import CatalogProduct
from app.services.gifting.embeddings import cosine
from app.services.gifting.profile import GiftProfile

MIN_GIFTABLE = 0.4
# A product may sit below the budget floor by up to 40% (a $17 item still suits
# a "$25–50" shopper) but never above the ceiling.
BUDGET_FLOOR_SLACK = 0.6

WEIGHTS = {"similarity": 0.55, "recipient_occasion": 0.20, "vibes": 0.15, "giftable": 0.10}
MMR_LAMBDA = 0.7
CANDIDATE_POOL = 60
DEFAULT_LIMIT = 12


@dataclass
class Intake:
    recipient: str
    occasion: str
    budget_band: str
    vibes: list[str] = field(default_factory=list)
    age_band: str | None = None
    free_text: str = ""
    exclude_ids: list[str] = field(default_factory=list)
    locale: str | None = None          # storefront language: reasons are written in it


@dataclass
class IndexedProduct:
    product: CatalogProduct
    profile: GiftProfile
    vector: list[float]


@dataclass
class Candidate:
    product: CatalogProduct
    profile: GiftProfile
    vector: list[float]
    score: float
    similarity: float


def query_text(intake: Intake) -> str:
    """The shopper's answers as a gift brief, embedded like a product profile."""
    lo, hi = vocab.budget_range(intake.budget_band)
    budget = f"under {hi}" if not lo and hi else (f"over {lo}" if hi is None else f"{lo} to {hi}")
    vibes = ", ".join(vocab.LABELS.get(v, v) for v in intake.vibes)
    parts = [
        f"A {vocab.LABELS.get(intake.occasion, intake.occasion)} gift for a "
        f"{vocab.LABELS.get(intake.recipient, intake.recipient)}",
        f"who is {vibes}" if vibes else "",
        f"budget {budget}",
        intake.free_text.strip(),
    ]
    return ". ".join(p for p in parts if p)


def in_budget(p: CatalogProduct, budget_band: str) -> bool:
    lo, hi = vocab.budget_range(budget_band)
    price_max = p.price_max or p.price_min
    if hi is not None and p.price_min > hi:
        return False
    return price_max >= lo * BUDGET_FLOOR_SLACK


def passes_filters(item: IndexedProduct, intake: Intake) -> bool:
    p, prof = item.product, item.profile
    if not p.available or p.excluded or p.product_id in intake.exclude_ids:
        return False
    if prof.giftable < MIN_GIFTABLE:
        return False
    if intake.age_band and intake.age_band != "any" and prof.age_band not in (intake.age_band, "any"):
        return False
    return in_budget(p, intake.budget_band)


def _match(value: str, values: list[str]) -> float:
    if value in values:
        return 1.0
    return 0.5 if "anyone" in values or "just_because" in values else 0.0


def score(item: IndexedProduct, intake: Intake, query_vector: list[float]) -> tuple[float, float]:
    sim = max(0.0, cosine(query_vector, item.vector))
    prof = item.profile
    rec_occ = (_match(intake.recipient, prof.recipients) + _match(intake.occasion, prof.occasions)) / 2
    vibes = len(set(intake.vibes) & set(prof.vibes)) / len(intake.vibes) if intake.vibes else 0.0
    total = (WEIGHTS["similarity"] * sim + WEIGHTS["recipient_occasion"] * rec_occ
             + WEIGHTS["vibes"] * vibes + WEIGHTS["giftable"] * prof.giftable)
    return total, sim


def _redundancy(c: Candidate, chosen: list[Candidate]) -> float:
    worst = 0.0
    for s in chosen:
        same_kind = 1.0 if (c.product.product_type and c.product.product_type == s.product.product_type) else 0.0
        same_vendor = 0.5 if (c.product.vendor and c.product.vendor == s.product.vendor) else 0.0
        worst = max(worst, 0.5 * max(0.0, cosine(c.vector, s.vector)) + 0.4 * same_kind + 0.1 * same_vendor)
    return worst


def diversify(candidates: list[Candidate], limit: int, lam: float = MMR_LAMBDA) -> list[Candidate]:
    """Maximal marginal relevance: trade a little score for variety."""
    pool = list(candidates)
    chosen: list[Candidate] = []
    while pool and len(chosen) < limit:
        best = max(pool, key=lambda c: lam * c.score - (1 - lam) * _redundancy(c, chosen))
        chosen.append(best)
        pool.remove(best)
    return chosen


def retrieve(
    intake: Intake,
    items: list[IndexedProduct],
    query_vector: list[float],
    limit: int = DEFAULT_LIMIT,
    pool_size: int = CANDIDATE_POOL,
) -> list[Candidate]:
    scored = []
    for it in items:
        if not passes_filters(it, intake):
            continue
        total, sim = score(it, intake, query_vector)
        scored.append(Candidate(it.product, it.profile, it.vector, total, sim))
    scored.sort(key=lambda c: c.score, reverse=True)
    # Items under the band floor (allowed by BUDGET_FLOOR_SLACK) only top up a
    # thin band; a "$25-50" shopper shouldn't see $16 gifts ahead of $30 ones.
    lo, _ = vocab.budget_range(intake.budget_band)
    in_band = [c for c in scored if (c.product.price_max or c.product.price_min) >= lo]
    if len(in_band) >= limit:
        return diversify(in_band[:pool_size], limit)
    picked = diversify(in_band, limit)
    below = [c for c in scored if (c.product.price_max or c.product.price_min) < lo]
    return picked + diversify(below[:pool_size], limit - len(picked))
