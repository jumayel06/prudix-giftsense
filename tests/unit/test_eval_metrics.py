"""Offline gift-finder evaluation: scoring math (docs/TECHNICAL_PLAN.md §10.3)."""
import pytest

from evals.metrics import SearchOutcome, cost_usd, score_search, summarize


def outcome(**kw):
    base = dict(persona_id="p1", catalog="candles", model="gpt-6-luna",
                picked=["a", "b", "c", "d", "e"], acceptable={"a", "b", "c"},
                budget_violations=0, invented=0, reasons_total=5, reasons_faithful=5,
                distinct_types=3, latency_ms=1800, input_tokens=2500, output_tokens=400)
    base.update(kw)
    return SearchOutcome(**base)


def test_score_search_counts_relevant_picks_out_of_five():
    s = score_search(outcome())
    assert s["relevant_at_5"] == 3


def test_fewer_than_five_picks_scale_to_five():
    s = score_search(outcome(picked=["a", "x", "y"], acceptable={"a"}))
    assert s["relevant_at_5"] == pytest.approx(5 / 3)


def test_no_picks_scores_zero():
    assert score_search(outcome(picked=[], acceptable={"a"}))["relevant_at_5"] == 0


def test_cost_uses_model_prices():
    assert cost_usd("gpt-6-luna", 2500, 400) == pytest.approx(0.00045)
    assert cost_usd("claude-sonnet-5", 2500, 400) == pytest.approx(0.009)


def test_summarize_per_model_with_pass_fail():
    outs = [outcome(), outcome(persona_id="p2", picked=["a", "b", "c", "d", "x"], acceptable={"a", "b", "c", "d"})]
    summary = summarize(outs)
    row = summary["gpt-6-luna"]
    assert row["searches"] == 2
    assert row["avg_relevant_at_5"] == pytest.approx(3.5)
    assert row["budget_violations"] == 0 and row["invented"] == 0
    assert row["faithfulness"] == pytest.approx(1.0)
    assert row["cost_per_search"] == pytest.approx(0.00045)
    assert row["passes"] is True


def test_any_budget_violation_or_invented_product_fails_the_model():
    assert summarize([outcome(budget_violations=1)])["gpt-6-luna"]["passes"] is False
    assert summarize([outcome(invented=1)])["gpt-6-luna"]["passes"] is False


def test_low_relevance_fails():
    assert summarize([outcome(acceptable={"a"})])["gpt-6-luna"]["passes"] is False


def test_unjudged_searches_are_left_out_of_relevance():
    # The judge failed for p2 (no labels): it must not count as 0 good picks.
    outs = [outcome(), outcome(persona_id="p2", acceptable=set(), judged=False)]
    assert summarize(outs)["gpt-6-luna"]["avg_relevant_at_5"] == pytest.approx(3)


def test_share_of_possible_accounts_for_thin_catalogs():
    # Only 2 acceptable products exist in the pool; picking both is a perfect score.
    s = score_search(outcome(picked=["a", "b", "x", "y", "z"], acceptable={"a", "b"}))
    assert s["share_of_possible"] == pytest.approx(1.0)
    assert score_search(outcome(acceptable=set()))["share_of_possible"] is None
