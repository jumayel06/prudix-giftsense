"""Gift profile: parse/validate the enrichment LLM's JSON, product text, embed text."""
from app.services.gifting.catalog import CatalogProduct
from app.services.gifting.profile import (
    GiftProfile,
    apply_overrides,
    embedding_text,
    fallback_profile,
    parse_profile,
    product_source_text,
)


def _product(**kw):
    base = dict(
        product_id="1", title="Merino Throw Blanket", description="<p>Heavyweight <b>merino</b> wool.</p>",
        product_type="Blankets", vendor="Loom & Co", tags=["cozy", "home", "wool"],
        price_min=89.0, price_max=89.0, available=True,
    )
    base.update(kw)
    return CatalogProduct(**base)


def test_parse_profile_keeps_valid_fields():
    p = parse_profile({
        "giftable": 0.9,
        "recipients": ["parent", "partner"],
        "occasions": ["birthday", "holiday"],
        "vibes": ["cozy", "luxurious"],
        "interests": ["Reading", "home"],
        "age_band": "adult",
        "gift_pitch": "A heavy merino throw for someone always cold on the couch.",
        "facts": ["100% merino wool", "50x60 in"],
    })
    assert p.giftable == 0.9
    assert p.recipients == ["parent", "partner"]
    assert p.vibes == ["cozy", "luxurious"]
    assert p.interests == ["reading", "home"]
    assert p.age_band == "adult"
    assert p.facts == ["100% merino wool", "50x60 in"]


def test_parse_profile_drops_off_vocabulary_and_clamps():
    p = parse_profile({
        "giftable": 7,                      # out of range → clamped
        "recipients": ["mom", "parent"],    # "mom" not in vocabulary
        "occasions": ["xmas"],              # not in vocabulary
        "vibes": "cozy",                    # wrong type → ignored
        "age_band": "elderly",              # unknown → "any"
        "gift_pitch": "x" * 500,
        "facts": ["ok"] + ["y" * 300] + [f"f{i}" for i in range(20)],
    })
    assert p.giftable == 1.0
    assert p.recipients == ["parent"]
    assert p.occasions == []
    assert p.vibes == []
    assert p.age_band == "any"
    assert len(p.gift_pitch) <= 200
    assert len(p.facts) <= 8
    assert all(len(f) <= 120 for f in p.facts)


def test_parse_profile_garbage_returns_none():
    assert parse_profile(None) is None
    assert parse_profile("not a dict") is None
    assert parse_profile({"giftable": "high"}) is not None  # non-numeric giftable → default, not a crash


def test_product_source_text_strips_html_and_includes_facts():
    text = product_source_text(_product())
    assert "<p>" not in text and "<b>" not in text
    assert "Merino Throw Blanket" in text and "merino wool" in text
    assert "89" in text and "Blankets" in text


def test_embedding_text_uses_profile_not_raw_html():
    prod = _product()
    prof = GiftProfile(giftable=0.9, recipients=["parent"], occasions=["birthday"], vibes=["cozy"],
                       interests=["home"], age_band="adult",
                       gift_pitch="A heavy throw for cold evenings.", facts=["merino"])
    text = embedding_text(prod, prof)
    assert "A heavy throw for cold evenings." in text
    assert "Parent" in text and "Birthday" in text and "Cozy" in text
    assert "<p>" not in text


def test_fallback_profile_from_tags_when_llm_unavailable():
    p = fallback_profile(_product(tags=["Cozy", "anniversary", "gift for mom"]))
    assert p.vibes == ["cozy"]
    assert p.occasions == ["anniversary"]
    assert 0 < p.giftable <= 1
    assert p.gift_pitch  # never empty


def test_merchant_overrides_win():
    prof = GiftProfile(giftable=0.2, recipients=["friend"], occasions=[], vibes=["funny"],
                       interests=[], age_band="adult", gift_pitch="x", facts=[])
    merged = apply_overrides(prof, {"giftable": 0.95, "recipients": ["parent", "bogus"], "vibes": []})
    assert merged.giftable == 0.95
    assert merged.recipients == ["parent"]
    assert merged.vibes == []
    assert merged.age_band == "adult"  # untouched


def test_merchant_can_override_the_pitch():
    from app.services.gifting.profile import GiftProfile, apply_overrides
    prof = apply_overrides(GiftProfile(gift_pitch="AI words"), {"gift_pitch": "  Our   words  "})
    assert prof.gift_pitch == "Our words"
    long = apply_overrides(GiftProfile(gift_pitch="x"), {"gift_pitch": "y" * 500})
    assert len(long.gift_pitch) == 200
