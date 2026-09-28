"""Scoring for the offline gift-finder evaluation (docs/TECHNICAL_PLAN.md §10.3).

Exit criteria per model: avg relevant picks ≥ 3.5 of 5, zero budget
violations, zero invented products, reason faithfulness ≥ 95%, diversity ≥ 3
product types in the top 5 (where the store has ≥ 5 types), p95 latency < 3.5s.
"""
from dataclasses import dataclass, field
from statistics import mean

from app.llm import calc_cost

TARGET_RELEVANT_AT_5 = 3.5
TARGET_FAITHFULNESS = 0.95
TARGET_DISTINCT_TYPES = 3
TARGET_P95_LATENCY_MS = 3500


@dataclass
class SearchOutcome:
    persona_id: str
    catalog: str
    model: str
    picked: list[str]
    acceptable: set[str] = field(default_factory=set)
    budget_violations: int = 0
    invented: int = 0
    reasons_total: int = 0
    reasons_faithful: int = 0
    distinct_types: int = 0
    catalog_has_5_types: bool = True
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    used_fallback: bool = False


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    return calc_cost(model, input_tokens, output_tokens)


def score_search(o: SearchOutcome) -> dict:
    """Relevant picks normalized to a 5-pick scale (3 of 3 counts as 5 of 5 —
    returning fewer picks isn't penalized, picking badly is)."""
    if not o.picked:
        return {"relevant_at_5": 0.0}
    hits = sum(1 for pid in o.picked[:5] if pid in o.acceptable)
    return {"relevant_at_5": hits * 5 / min(5, len(o.picked))}


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]


def summarize(outcomes: list[SearchOutcome]) -> dict[str, dict]:
    by_model: dict[str, list[SearchOutcome]] = {}
    for o in outcomes:
        by_model.setdefault(o.model, []).append(o)

    summary = {}
    for model, outs in by_model.items():
        rel = [score_search(o)["relevant_at_5"] for o in outs]
        reasons_total = sum(o.reasons_total for o in outs)
        typed = [o.distinct_types for o in outs if o.catalog_has_5_types]
        row = {
            "searches": len(outs),
            "avg_relevant_at_5": mean(rel) if rel else 0.0,
            "budget_violations": sum(o.budget_violations for o in outs),
            "invented": sum(o.invented for o in outs),
            "faithfulness": (sum(o.reasons_faithful for o in outs) / reasons_total) if reasons_total else 1.0,
            "avg_distinct_types": mean(typed) if typed else None,
            "p95_latency_ms": _p95([o.latency_ms for o in outs]),
            "fallback_rate": sum(o.used_fallback for o in outs) / len(outs),
            "cost_per_search": mean(cost_usd(model, o.input_tokens, o.output_tokens) for o in outs),
            "avg_input_tokens": mean(o.input_tokens for o in outs),
            "avg_output_tokens": mean(o.output_tokens for o in outs),
        }
        row["passes"] = (
            row["avg_relevant_at_5"] >= TARGET_RELEVANT_AT_5
            and row["budget_violations"] == 0
            and row["invented"] == 0
            and row["faithfulness"] >= TARGET_FAITHFULNESS
            and (row["avg_distinct_types"] is None or row["avg_distinct_types"] >= TARGET_DISTINCT_TYPES)
            and row["p95_latency_ms"] < TARGET_P95_LATENCY_MS
        )
        summary[model] = row
    return summary
