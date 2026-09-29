"""The gift-finder intake as sent by the storefront widget and the dashboard's
Try it page. Values outside the fixed vocabularies are rejected (422)."""
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.services.gifting import vocab
from app.services.gifting.rerank import budget_label

MAX_VIBES = 3
MAX_FREE_TEXT = 200


class GiftBrief(BaseModel):
    """A gift-finder intake, validated against the fixed vocabularies."""
    recipient: str
    occasion: str
    budget_band: str
    vibes: list[str] = Field(default_factory=list, max_length=MAX_VIBES)
    age_band: Optional[str] = None
    free_text: str = Field(default="", max_length=MAX_FREE_TEXT)

    @field_validator("recipient")
    @classmethod
    def _recipient(cls, v):
        if v not in vocab.RECIPIENTS:
            raise ValueError("unknown recipient")
        return v

    @field_validator("occasion")
    @classmethod
    def _occasion(cls, v):
        if v not in vocab.OCCASIONS:
            raise ValueError("unknown occasion")
        return v

    @field_validator("budget_band")
    @classmethod
    def _budget(cls, v):
        if v not in vocab.BUDGET_BANDS:
            raise ValueError("unknown budget")
        return v

    @field_validator("vibes")
    @classmethod
    def _vibes(cls, v):
        if any(x not in vocab.VIBES for x in v):
            raise ValueError("unknown vibe")
        return v

    @field_validator("age_band")
    @classmethod
    def _age(cls, v):
        if v is not None and v not in vocab.AGE_BANDS:
            raise ValueError("unknown age band")
        return v


def intake_options() -> dict:
    """The intake form's choices (labels for display), for the widget and Try it."""
    def opts(values):
        return [{"value": v, "label": vocab.LABELS.get(v, v)} for v in values]
    return {
        "recipients": opts(vocab.RECIPIENTS),
        "occasions": opts(vocab.OCCASIONS),
        "vibes": opts(vocab.VIBES),
        "age_bands": opts(vocab.AGE_BANDS),
        "budgets": [{"value": b, "label": budget_label(b)} for b in vocab.BUDGET_BANDS],
        "max_vibes": MAX_VIBES,
        "max_free_text": MAX_FREE_TEXT,
    }
