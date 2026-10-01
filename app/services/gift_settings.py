"""Merchant gift-note settings (Settings page), stored in shops.gift_settings.

tone        default voice for AI note drafts (shoppers can switch per draft)
max_chars   note length cap; 250 fits a printed gift note card
banned_words words a draft must never contain (checked case-insensitively)
"""
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

NOTE_TONES = {
    "warm": "Warm and casual",
    "elegant": "Minimal and elegant",
    "playful": "Playful",
    "formal": "Formal",
}
DEFAULTS = {"tone": "warm", "max_chars": 250, "banned_words": []}
MIN_CHARS, MAX_CHARS = 80, 500
MAX_BANNED, MAX_BANNED_LEN = 50, 40

Tone = Literal["warm", "elegant", "playful", "formal"]


class GiftNotesUpdate(BaseModel):
    tone: Optional[Tone] = None
    max_chars: Optional[int] = Field(default=None, ge=MIN_CHARS, le=MAX_CHARS)
    banned_words: Optional[list[str]] = Field(default=None, max_length=MAX_BANNED)

    @field_validator("banned_words")
    @classmethod
    def _clean(cls, words):
        if words is None:
            return None
        out: list[str] = []
        for w in words:
            w = " ".join(str(w).split()).lower()
            if len(w) > MAX_BANNED_LEN:
                raise ValueError(f"banned words can be at most {MAX_BANNED_LEN} characters")
            if w and w not in out:
                out.append(w)
        return out


def note_settings(shop) -> dict:
    stored = (getattr(shop, "gift_settings", None) or {}).get("notes") or {}
    return {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}


def update_note_settings(shop, update: GiftNotesUpdate) -> None:
    current = note_settings(shop)
    current.update(update.model_dump(exclude_none=True))
    shop.gift_settings = {**(shop.gift_settings or {}), "notes": current}
