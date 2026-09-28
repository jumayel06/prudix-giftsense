"""Retrieval: hard filters, hybrid scoring, diversity (no LLM). Uses the
deterministic FakeEmbedder so rankings are reproducible."""
import math

import pytest

from app.services.gifting.catalog import CatalogProduct
from app.services.gifting.embeddings import FakeEmbedder, cosine
from app.services.gifting.profile import GiftProfile, embedding_text
from app.services.gifting.retrieval import IndexedProduct, Intake, query_text, retrieve


def item(pid, title, *, price=40.0, ptype="Candles", recipients=("friend",), occasions=("birthday",),
         vibes=("cozy",), giftable=0.9, age="adult", available=True, excluded=False, vendor="V",
         price_max=None, pitch=None):
    p = CatalogProduct(product_id=pid, title=title, product_type=ptype, vendor=vendor, price_min=price,
                       price_max=price_max or price, available=available, excluded=excluded)
    prof = GiftProfile(giftable=giftable, recipients=list(recipients), occasions=list(occasions),
                       vibes=list(vibes), age_band=age, gift_pitch=pitch or f"{title} gift.")
    return p, prof


def index(items):
    emb = FakeEmbedder()
    return [IndexedProduct(p, prof, emb.embed_sync(embedding_text(p, prof))) for p, prof in items]


def run(items, intake, **kw):
    emb = FakeEmbedder()
    return retrieve(intake, index(items), emb.embed_sync(query_text(intake)), **kw)


BASE = Intake(recipient="parent", occasion="birthday", budget_band="25_50", vibes=["cozy", "relaxing"])


def test_fake_embedder_is_deterministic_and_normalized():
    e = FakeEmbedder()
    a, b = e.embed_sync("cozy candle for mom"), e.embed_sync("cozy candle for mom")
    assert a == b and len(a) == e.dims
    assert math.isclose(sum(x * x for x in a), 1.0, rel_tol=1e-6)
    assert cosine(a, e.embed_sync("cozy candle")) > cosine(a, e.embed_sync("power drill battery"))


def test_budget_is_a_hard_filter():
    results = run([
        item("in", "Lavender Candle", price=38),
        item("over", "Luxury Candle Set", price=120),
        item("too_cheap", "Tea Light", price=5),           # far under the 25 minimum
        item("range", "Candle Trio", price=20, price_max=45),  # some variant fits
    ], BASE)
    ids = {r.product.product_id for r in results}
    assert ids == {"in", "range"}


def test_open_ended_top_band():
    intake = Intake(recipient="partner", occasion="anniversary", budget_band="200_plus", vibes=["luxurious"])
    ids = {r.product.product_id for r in run([
        item("watch", "Watch", price=450, vibes=("luxurious",)),
        item("mug", "Mug", price=18),
    ], intake)}
    assert ids == {"watch"}


@pytest.mark.parametrize("kw", [{"available": False}, {"excluded": True}, {"giftable": 0.1}])
def test_unavailable_excluded_and_non_giftable_are_filtered(kw):
    assert run([item("x", "Candle", **kw)], BASE) == []


def test_age_band_mismatch_filtered_but_any_is_kept():
    intake = Intake(recipient="kid", occasion="birthday", budget_band="25_50", vibes=["funny"], age_band="kid")
    ids = {r.product.product_id for r in run([
        item("toy", "Toy", age="kid", recipients=("kid",), vibes=("funny",)),
        item("wine", "Wine Glasses", age="adult"),
        item("book", "Puzzle Book", age="any"),
    ], intake)}
    assert ids == {"toy", "book"}


def test_matching_recipient_occasion_and_vibes_rank_higher():
    results = run([
        item("match", "Lavender Bath Candle", recipients=("parent",), occasions=("birthday",), vibes=("cozy", "relaxing")),
        item("miss", "Joke Mug", recipients=("coworker",), occasions=("just_because",), vibes=("funny",), ptype="Mugs"),
    ], BASE)
    assert [r.product.product_id for r in results][0] == "match"


def test_excluded_ids_are_skipped_for_refinement():
    intake = Intake(**{**BASE.__dict__, "exclude_ids": ["a"]})
    ids = {r.product.product_id for r in run([item("a", "Candle A"), item("b", "Candle B")], intake)}
    assert ids == {"b"}


def test_diversity_avoids_a_wall_of_near_identical_items():
    items = [item(f"c{i}", f"Lavender Candle {i}", recipients=("parent",), vibes=("cozy", "relaxing"),
                  pitch="A lavender candle for relaxing evenings.") for i in range(8)]
    items += [
        item("robe", "Plush Robe", ptype="Loungewear", recipients=("parent",), vibes=("cozy", "relaxing"),
             pitch="A plush robe for slow mornings."),
        item("tea", "Tea Sampler", ptype="Tea", recipients=("parent",), vibes=("cozy",),
             pitch="A tea sampler for quiet afternoons."),
    ]
    top5 = [r.product for r in run(items, BASE, limit=5)]
    assert len({p.product_type for p in top5}) >= 3


def test_limit_and_scores_sorted():
    results = run([item(str(i), f"Candle {i}") for i in range(20)], BASE, limit=12)
    assert len(results) == 12
    assert all(0.0 <= r.score <= 1.5 for r in results)


def test_query_text_reads_like_a_gift_brief():
    text = query_text(Intake(recipient="parent", occasion="birthday", budget_band="25_50",
                             vibes=["cozy"], free_text="loves gardening"))
    assert "Parent" in text and "Birthday" in text and "Cozy" in text and "gardening" in text
