#!/usr/bin/env python3
"""Offline gift-finder evaluation (internal tooling; spends real API money).

    .venv/bin/python scripts/eval_recs.py --stores candles,general --models haiku,sonnet --cap 6

Stores: candles, jewelry, toys, kitchen, general.
Models: mini (gpt-4o-mini), haiku (claude-haiku-4-5), gpt41 (gpt-4.1), sonnet (claude-sonnet-5).
Outputs evals/results/report.md, report.json, review.html. Catalogs, profiles
and judge labels are cached under evals/data/, so reruns only pay for searches.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.gifting.embeddings import OpenAIEmbedder  # noqa: E402
from evals.catalogs import STORES  # noqa: E402
from evals.ledger import BudgetExceeded, Ledger  # noqa: E402
from evals.runner import DATA, RESULTS, EvalRun  # noqa: E402

MODELS = {"mini": "gpt-4o-mini", "haiku": "claude-haiku-4-5", "gpt41": "gpt-4.1", "sonnet": "claude-sonnet-5"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stores", default="candles,general")
    ap.add_argument("--models", default="haiku,sonnet")
    ap.add_argument("--cap", type=float, required=True, help="hard spending cap in USD")
    args = ap.parse_args()

    stores = [s.strip() for s in args.stores.split(",") if s.strip()]
    models = [MODELS[m.strip()] for m in args.models.split(",") if m.strip()]
    unknown = [s for s in stores if s not in STORES]
    if unknown:
        print(f"Unknown stores: {unknown}. Choose from {list(STORES)}")
        return 2

    personas = json.loads((Path(__file__).resolve().parents[1] / "evals" / "personas.json").read_text())
    new = [s for s in stores if not (DATA / "catalogs" / f"{s}.json").exists()]
    print(f"Stores: {stores} (new catalogs to generate: {new or 'none'})")
    print(f"Models: {models}")
    print(f"Searches: {sum(len(personas[s]) for s in stores) * len(models)}  ·  cap ${args.cap:.2f}\n")

    ledger = Ledger(cap_usd=args.cap)
    run = EvalRun(stores, models, ledger, OpenAIEmbedder())
    try:
        report = asyncio.run(run.run({s: personas[s] for s in stores}))
    except BudgetExceeded as e:
        print(f"\n{e}\nSpent by step: {ledger.by_step()}")
        return 1
    print((RESULTS / "report.md").read_text())
    print(f"Spot-check page: {RESULTS / 'review.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
