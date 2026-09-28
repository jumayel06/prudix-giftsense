"""Every test shopper must use valid vocabulary and budget bands."""
import json
from pathlib import Path

from app.services.gifting import vocab

PERSONAS = json.loads((Path(__file__).resolve().parents[2] / "evals" / "personas.json").read_text())


def test_ten_personas_per_store_with_unique_ids_and_valid_vocabulary():
    ids = set()
    for store, personas in PERSONAS.items():
        if store.startswith("_"):
            continue
        assert len(personas) == 10, store
        for p in personas:
            assert p["id"] not in ids
            ids.add(p["id"])
            assert p["recipient"] in vocab.RECIPIENTS, p
            assert p["occasion"] in vocab.OCCASIONS, p
            vocab.budget_range(p["budget_band"])
            assert p["vibes"] and all(v in vocab.VIBES for v in p["vibes"]), p
            assert p.get("age_band") in (None, *vocab.AGE_BANDS), p
