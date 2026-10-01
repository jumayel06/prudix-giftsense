"""AI-drafted gift notes (docs/TECHNICAL_PLAN.md §5.2).

One short LLM call per draft, then guards: links, emails and phone numbers
removed; capped at the merchant's length on a word boundary; banned words
retried once; output moderation. Anything that fails → a tone-matched
template, so the shopper always gets a usable note. Drafts are never sent
anywhere automatically: the shopper edits and submits them.
"""
import json
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

import structlog

from app.llm import LLMResponse, chat, moderate
from app.services.gifting import vocab
from app.services.gift_settings import NOTE_TONES

logger = structlog.get_logger()

PROMPT_VERSION = "note-v2"
NOTE_TIMEOUT_SECS = 8.0

ChatFn = Callable[..., Awaitable[LLMResponse]]
ModerateFn = Callable[[str], Awaitable[bool]]

TONE_GUIDE = {
    "warm": "warm and casual, like a friend writing",
    "elegant": "minimal and elegant: short, graceful, understated",
    "playful": "playful and light-hearted, gently funny",
    "formal": "formal and polite, suitable for colleagues or clients",
}

SYSTEM_PROMPT = """You write the short message that goes on a printed gift note.

Write in a {tone} tone, at most {max_chars} characters. Write to the recipient
(use their name if given). Mention the occasion; you may nod to the gift, using
only the details given. Do not sign it (the sender adds their name), and no
links, emails, phone numbers, hashtags or emojis. Never use these words: {banned}.

Return JSON only: {{"note": "..."}}"""

_URL = re.compile(r"(https?://\S+|www\.\S+|\b[\w-]+\.(com|net|org|io|co|shop|store)\b\S*)", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"\+?\d[\d\s().-]{6,}\d")


@dataclass
class NoteResult:
    text: str
    source: str            # "ai" | "template"
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


def clean_note(text: str, max_chars: int) -> str:
    text = _URL.sub("", _EMAIL.sub("", str(text or "")))   # emails first: the URL rule would eat their domain
    text = _PHONE.sub("", text)
    text = " ".join(text.split()).strip().strip('"“”').strip()
    text = re.sub(r"\s+([,.!?])", r"\1", text)
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars + 1]
    # Prefer ending on a sentence, else on a word.
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= max_chars // 2:
        return cut[:end + 1].strip()
    return cut[:cut.rfind(" ")].rstrip(",;:- ") if " " in cut else cut[:max_chars]


def contains_banned(text: str, banned: list[str]) -> bool:
    low = text.lower()
    return any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", low) for w in banned if w)


_TEMPLATES = {
    "warm": "{hi}Wishing you the happiest {occasion}. I hope this little gift makes you smile!",
    "elegant": "{hi}With warm wishes for your {occasion}.",
    "playful": "{hi}Surprise! A little something to make your {occasion} extra fun.",
    "formal": "{hi}Please accept this gift with my best wishes for your {occasion}.",
}
_OCCASION_WORDS = {
    "birthday": "birthday", "anniversary": "anniversary", "holiday": "holidays", "thank_you": "day",
    "wedding": "wedding day", "new_baby": "new arrival", "housewarming": "new home", "graduation": "graduation",
    "get_well": "recovery", "sympathy": "days ahead", "retirement": "retirement", "just_because": "day",
}


def template_note(tone: str, recipient: str | None, occasion: str | None, name: str, max_chars: int) -> str:
    hi = f"Dear {name}, " if name and tone in ("elegant", "formal") else (f"{name}, " if name else "")
    text = _TEMPLATES.get(tone, _TEMPLATES["warm"]).format(hi=hi, occasion=_OCCASION_WORDS.get(occasion or "", "day"))
    if occasion == "thank_you":
        text = f"{hi}Thank you so much. I hope you enjoy this little gift."
    elif occasion == "sympathy":
        text = f"{hi}Thinking of you, with love."
    return clean_note(text, max_chars)


def _prompt(recipient, occasion, name, product_title, product_pitch, product_facts, banned) -> str:
    lines = [
        f"Recipient: {vocab.LABELS.get(recipient, 'someone special') if recipient else 'someone special'}",
        f"Occasion: {vocab.LABELS.get(occasion, 'just because') if occasion else 'just because'}",
    ]
    if name:
        lines.append(f"Recipient's first name: {name}")
    if product_title:
        lines.append(f"Gift: {product_title}")
        if product_pitch:
            lines.append(f"About the gift: {product_pitch}")
        if product_facts:
            lines.append(f"Gift details: {'; '.join(product_facts[:4])}")
    if banned:
        lines.append(f"Words to avoid: {', '.join(banned)}")
    return "\n".join(lines)


async def draft_note(
    *, recipient: str | None, occasion: str | None, tone: str, max_chars: int, banned_words: list[str],
    product_title: str = "", product_pitch: str = "", product_facts: list[str] | None = None, name: str = "",
    model: str, chat_fn: ChatFn = chat, moderate_fn: ModerateFn = moderate,
) -> NoteResult:
    tone = tone if tone in NOTE_TONES else "warm"
    system = SYSTEM_PROMPT.format(tone=TONE_GUIDE[tone], max_chars=max_chars,
                                  banned=", ".join(banned_words) if banned_words else "(none)")
    prompt = _prompt(recipient, occasion, name, product_title, product_pitch, product_facts or [], banned_words)
    tokens_in = tokens_out = 0
    used_model = model
    fallback = NoteResult(template_note(tone, recipient, occasion, name, max_chars), "template")

    for attempt in range(2):
        try:
            resp = await chat_fn(model=model, system=system, prompt=prompt, max_tokens=300, temperature=0.8,
                                 json_mode=True, timeout=NOTE_TIMEOUT_SECS)
        except Exception as e:  # noqa: BLE001 — the shopper still gets a note
            logger.warning("gift_note_llm_failed", model=model, error=str(e)[:200])
            break
        tokens_in += resp.input_tokens
        tokens_out += resp.output_tokens
        used_model = getattr(resp, "model", "") or model
        try:
            text = clean_note(json.loads(resp.text).get("note", ""), max_chars)
        except (ValueError, AttributeError):
            text = ""
        if not text:
            break
        if contains_banned(text, banned_words):
            prompt += f"\nYour last draft used a banned word. Do not use: {', '.join(banned_words)}."
            continue
        if await moderate_fn(text):
            logger.warning("gift_note_flagged", model=model)
            break
        return NoteResult(text, "ai", tokens_in, tokens_out, used_model)

    fallback.input_tokens, fallback.output_tokens, fallback.model = tokens_in, tokens_out, used_model
    return fallback
