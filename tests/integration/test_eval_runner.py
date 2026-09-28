"""Offline eval runner end to end with a fake model ($0): catalog generation,
profiles, vectors, searches, judge labels, report, review page, caching."""
import json

import pytest

from app.llm import LLMResponse
from app.services.gifting.embeddings import FakeEmbedder
from evals.ledger import Ledger
from evals.runner import EvalRun


class FakeModel:
    """Answers by prompt type, like the real models would, and counts calls."""

    def __init__(self):
        self.calls = {"generate": 0, "enrich": 0, "rerank": 0, "judge": 0}

    async def __call__(self, *, model, system, prompt, **kw):
        if "realistic product listings" in system:
            self.calls["generate"] += 1
            n = int(prompt.split("Write ")[1].split(" ")[0])
            start = self.calls["generate"] * 100
            items = [{"title": f"Soy Candle {start + i}", "product_type": ["Candles", "Gift Sets", "Wax Melts"][i % 3],
                      "vendor": "Wick", "description_html": "<p>Hand-poured soy wax candle.</p>",
                      "tags": ["cozy", "birthday"], "price_min": 20 + i % 25, "price_max": 20 + i % 25} for i in range(n)]
            return LLMResponse(json.dumps({"products": items}), 600, 250 * n)
        if "describe a store product as a potential gift" in system:
            self.calls["enrich"] += 1
            return LLMResponse(json.dumps({"giftable": 0.9, "recipients": ["parent", "friend"],
                                           "occasions": ["birthday"], "vibes": ["cozy", "relaxing"],
                                           "gift_pitch": "A calming candle.", "facts": ["soy wax"]}), 750, 200)
        if "grading a gift finder" in system:
            self.calls["judge"] += 1
            ids = [line.split(" | ")[0][2:] for line in prompt.splitlines() if line.startswith("- candles-")]
            return LLMResponse(json.dumps({"acceptable": ids[: len(ids) // 2], "unfaithful": []}), 3000, 200)
        self.calls["rerank"] += 1
        ids = [line.split("product_id: ")[1].split(" |")[0] for line in prompt.splitlines() if "product_id: " in line]
        return LLMResponse(json.dumps({"picks": [{"product_id": i, "reason": "A calming soy candle."} for i in ids[:4]]}),
                           2500, 300)


PERSONAS = {"candles": [
    {"id": "cn01", "recipient": "parent", "occasion": "birthday", "budget_band": "25_50", "vibes": ["cozy"]},
    {"id": "cn02", "recipient": "friend", "occasion": "birthday", "budget_band": "under_25", "vibes": ["relaxing"]},
]}


@pytest.mark.asyncio
async def test_full_run_offline_then_rerun_uses_caches(tmp_path):
    model = FakeModel()
    run = EvalRun(["candles"], ["claude-haiku-4-5", "claude-sonnet-5"], Ledger(cap_usd=5), FakeEmbedder(),
                  chat_fn=model, data_dir=tmp_path / "data", results_dir=tmp_path / "results", log=lambda *_: None)
    report = await run.run(PERSONAS)

    assert set(report["summary"]) == {"claude-haiku-4-5", "claude-sonnet-5"}
    row = report["summary"]["claude-haiku-4-5"]
    assert row["searches"] == 2 and row["budget_violations"] == 0 and row["invented"] == 0
    assert (tmp_path / "results" / "report.md").read_text().startswith("# Gift finder evaluation")
    assert "Label spot-check" in (tmp_path / "results" / "review.html").read_text()
    assert len(json.loads((tmp_path / "data" / "catalogs" / "candles.json").read_text())) == 25
    assert model.calls["enrich"] == 25 and model.calls["judge"] == 2
    assert report["spend"]["total"] > 0

    # Rerun: catalogs + profiles cached; only searches (and reason checks) cost again.
    before = dict(model.calls)
    run2 = EvalRun(["candles"], ["claude-haiku-4-5"], Ledger(cap_usd=5), FakeEmbedder(), chat_fn=model,
                   data_dir=tmp_path / "data", results_dir=tmp_path / "results", log=lambda *_: None)
    await run2.run(PERSONAS)
    assert model.calls["generate"] == before["generate"]
    assert model.calls["enrich"] == before["enrich"]
    assert model.calls["rerank"] == before["rerank"] + 2


@pytest.mark.asyncio
async def test_run_stops_at_the_spending_cap_and_keeps_finished_work(tmp_path):
    """A stop mid-catalog saves nothing (a partial catalog would later look
    complete); a stop mid-profiles keeps the profiles already written."""
    from evals.ledger import BudgetExceeded
    model = FakeModel()
    run = EvalRun(["candles"], ["claude-haiku-4-5"], Ledger(cap_usd=0.07), FakeEmbedder(), chat_fn=model,
                  data_dir=tmp_path / "data", results_dir=tmp_path / "results", concurrency=1, log=lambda *_: None)
    with pytest.raises(BudgetExceeded):
        await run.run(PERSONAS)
    assert len(json.loads((tmp_path / "data" / "catalogs" / "candles.json").read_text())) == 25
    saved = json.loads((tmp_path / "data" / "profiles" / "candles.json").read_text())
    assert 0 < len(saved) < 25
    assert run.ledger.spent <= 0.07


@pytest.mark.asyncio
async def test_stop_during_catalog_generation_saves_no_partial_catalog(tmp_path):
    from evals.ledger import BudgetExceeded
    run = EvalRun(["candles"], ["claude-haiku-4-5"], Ledger(cap_usd=0.04), FakeEmbedder(), chat_fn=FakeModel(),
                  data_dir=tmp_path / "data", results_dir=tmp_path / "results", log=lambda *_: None)
    with pytest.raises(BudgetExceeded):
        await run.run(PERSONAS)
    assert not (tmp_path / "data" / "catalogs" / "candles.json").exists()
