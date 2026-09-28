"""Gift profiles: the AI-written, vocabulary-constrained description of each
product that shopper intakes are matched against (docs/TECHNICAL_PLAN.md §4.2).

The enrichment LLM's output is untrusted: `parse_profile` keeps only
in-vocabulary values, clamps numbers and truncates text. `facts` are the only
product claims the reason-writer may later cite, which keeps reasons grounded.
"""
import html
import re
from dataclasses import dataclass, field, replace

from app.services.gifting import vocab
from app.services.gifting.catalog import CatalogProduct

MAX_PITCH_CHARS = 200
MAX_FACTS = 8
MAX_FACT_CHARS = 120
MAX_INTERESTS = 8
MAX_DESCRIPTION_CHARS = 2000


@dataclass
class GiftProfile:
    giftable: float = 0.5
    recipients: list[str] = field(default_factory=list)
    occasions: list[str] = field(default_factory=list)
    vibes: list[str] = field(default_factory=list)
    interests: list[str] = field(default_factory=list)
    age_band: str = "any"
    gift_pitch: str = ""
    facts: list[str] = field(default_factory=list)


def _clamp01(value, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _str_list(value) -> list:
    return value if isinstance(value, list) else []


def _short(text, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit].rstrip()


def parse_profile(data) -> GiftProfile | None:
    """Validate the enrichment LLM's JSON. Returns None for non-dict input."""
    if not isinstance(data, dict):
        return None
    interests: list[str] = []
    for v in _str_list(data.get("interests")):
        v = _short(v, 40).lower()
        if v and v not in interests:
            interests.append(v)
    facts = [_short(f, MAX_FACT_CHARS) for f in _str_list(data.get("facts")) if _short(f, MAX_FACT_CHARS)]
    age_band = str(data.get("age_band") or "").strip().lower()
    return GiftProfile(
        giftable=_clamp01(data.get("giftable")),
        recipients=vocab.clean_list(_str_list(data.get("recipients")), vocab.RECIPIENTS),
        occasions=vocab.clean_list(_str_list(data.get("occasions")), vocab.OCCASIONS),
        vibes=vocab.clean_list(_str_list(data.get("vibes")), vocab.VIBES),
        interests=interests[:MAX_INTERESTS],
        age_band=age_band if age_band in vocab.AGE_BANDS else "any",
        gift_pitch=_short(data.get("gift_pitch"), MAX_PITCH_CHARS),
        facts=facts[:MAX_FACTS],
    )


def strip_html(text: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _price_text(p: CatalogProduct) -> str:
    if p.price_max and p.price_max != p.price_min:
        return f"{p.price_min:.2f}–{p.price_max:.2f}"
    return f"{p.price_min:.2f}"


def product_source_text(p: CatalogProduct) -> str:
    """The product facts given to the enrichment LLM (HTML stripped, capped)."""
    parts = [
        f"Title: {p.title}",
        f"Type: {p.product_type}" if p.product_type else "",
        f"Vendor: {p.vendor}" if p.vendor else "",
        f"Price: {_price_text(p)}",
        f"Tags: {', '.join(p.tags)}" if p.tags else "",
        f"Description: {strip_html(p.description)[:MAX_DESCRIPTION_CHARS]}" if p.description else "",
    ]
    return "\n".join(x for x in parts if x)


def embedding_text(p: CatalogProduct, prof: GiftProfile) -> str:
    """What gets embedded: the gift profile (pitch + vocab labels), not raw HTML,
    so "cozy · parent · birthday" lands near the right products."""
    label = lambda values: ", ".join(vocab.LABELS.get(v, v) for v in values)  # noqa: E731
    parts = [
        prof.gift_pitch,
        f"For: {label(prof.recipients)}" if prof.recipients else "",
        f"Occasions: {label(prof.occasions)}" if prof.occasions else "",
        f"Vibe: {label(prof.vibes)}" if prof.vibes else "",
        f"Interests: {', '.join(prof.interests)}" if prof.interests else "",
        f"{p.product_type}: {p.title}" if p.product_type else p.title,
    ]
    return ". ".join(x for x in parts if x)


def fallback_profile(p: CatalogProduct) -> GiftProfile:
    """Profile from tags/title alone, used when enrichment fails, so a product
    is still matchable (lower confidence) instead of missing from the finder."""
    words = [str(t).strip().lower() for t in p.tags]
    return GiftProfile(
        giftable=0.5,
        recipients=vocab.clean_list(words, vocab.RECIPIENTS),
        occasions=vocab.clean_list(words, vocab.OCCASIONS),
        vibes=vocab.clean_list(words, vocab.VIBES),
        interests=[],
        age_band="any",
        gift_pitch=_short(p.title, MAX_PITCH_CHARS),
        facts=[],
    )


def apply_overrides(prof: GiftProfile, overrides: dict | None) -> GiftProfile:
    """Merchant edits from the Catalog page win over the AI's profile."""
    if not overrides:
        return prof
    changes = {}
    if "giftable" in overrides:
        changes["giftable"] = _clamp01(overrides["giftable"], prof.giftable)
    for key, allowed in (("recipients", vocab.RECIPIENTS), ("occasions", vocab.OCCASIONS), ("vibes", vocab.VIBES)):
        if key in overrides:
            changes[key] = vocab.clean_list(_str_list(overrides[key]), allowed)
    if "age_band" in overrides and overrides["age_band"] in vocab.AGE_BANDS:
        changes["age_band"] = overrides["age_band"]
    return replace(prof, **changes)
