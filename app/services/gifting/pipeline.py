"""Gift recommendation pipeline (docs/TECHNICAL_PLAN.md §4.4–4.5).

    intake → hard filters → (vector shortlist | whole small catalog) → rerank

Small-catalog mode: when a shop has ≤ SMALL_CATALOG_MAX products, skip the
query embedding and hand every eligible product to the reranker. That's both
cheaper and better for small stores (the spec's cold-start concern).
"""
from dataclasses import dataclass, field

from app.llm import chat
from app.services.gifting.embeddings import Embedder
from app.services.gifting.rerank import ChatFn, Pick, rerank
from app.services.gifting.retrieval import (
    Candidate, IndexedProduct, Intake, passes_filters, query_text, retrieve, score,
)

SMALL_CATALOG_MAX = 60


@dataclass
class Recommendation:
    picks: list[Pick] = field(default_factory=list)
    mode: str = "vector"            # "vector" | "small_catalog"
    used_fallback: bool = False
    candidates_considered: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


async def recommend(
    intake: Intake,
    items: list[IndexedProduct],
    embedder: Embedder,
    model: str,
    chat_fn: ChatFn = chat,
    use_llm: bool = True,
    query_vector: list[float] | None = None,
) -> Recommendation:
    """`query_vector` skips embedding the brief again when the caller already
    did (the DB index embeds it once to order its prefetch)."""
    if len(items) <= SMALL_CATALOG_MAX:
        mode = "small_catalog"
        no_vector: list[float] = []
        candidates = []
        for it in items:
            if passes_filters(it, intake):
                total, sim = score(it, intake, no_vector)
                candidates.append(Candidate(it.product, it.profile, it.vector, total, sim))
        candidates.sort(key=lambda c: c.score, reverse=True)
    else:
        mode = "vector"
        if query_vector is not None:
            qvec = query_vector
        else:
            [qvec] = await embedder.embed([query_text(intake)])
        candidates = retrieve(intake, items, qvec)

    result = await rerank(intake, candidates, model=model, chat_fn=chat_fn, use_llm=use_llm)
    return Recommendation(
        picks=result.picks, mode=mode, used_fallback=result.used_fallback,
        candidates_considered=len(candidates),
        input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        model=result.model or model,
    )
