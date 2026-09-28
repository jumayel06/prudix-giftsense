"""Fixed gift vocabularies shared by the shopper intake and product gift profiles.

Matching is exact because both sides use these words (docs/TECHNICAL_PLAN.md §4.2).
"""
import pytest

from app.services.gifting import vocab


def test_vocabularies_are_non_empty_unique_lowercase_slugs():
    for name in ("RECIPIENTS", "OCCASIONS", "VIBES", "AGE_BANDS"):
        values = getattr(vocab, name)
        assert values, name
        assert len(values) == len(set(values)), f"{name} has duplicates"
        for v in values:
            assert v == v.lower() and " " not in v, f"{name}: {v!r} must be a lowercase slug"


def test_every_vocabulary_value_has_a_shopper_label():
    for name in ("RECIPIENTS", "OCCASIONS", "VIBES", "AGE_BANDS"):
        for v in getattr(vocab, name):
            assert vocab.LABELS.get(v), f"missing label for {v}"


@pytest.mark.parametrize("band,expected", [
    ("under_25", (0, 25)),
    ("25_50", (25, 50)),
    ("50_100", (50, 100)),
    ("100_200", (100, 200)),
    ("200_plus", (200, None)),
])
def test_budget_bands_map_to_ranges(band, expected):
    assert vocab.budget_range(band) == expected


def test_unknown_budget_band_raises():
    with pytest.raises(ValueError):
        vocab.budget_range("cheap")


def test_clean_list_keeps_only_known_values_in_order_without_duplicates():
    assert vocab.clean_list(["cozy", "Funny ", "cozy", "spicy", "practical"], vocab.VIBES) == ["cozy", "funny", "practical"]
    assert vocab.clean_list(None, vocab.VIBES) == []
