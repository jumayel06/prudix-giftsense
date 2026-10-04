"""Shopper's own language: AI reasons/notes follow the storefront locale, and
the widget's text ships translated (extensions/giftsense-theme)."""
import json
import re
from pathlib import Path

import pytest

from app.services.gift_settings import NOTE_TONES
from app.services.gifting import languages, vocab
from app.services.gifting.brief import intake_options
from app.services.gifting.rerank import _prompt
from app.services.gifting.retrieval import Intake

EXT = Path(__file__).resolve().parents[2] / "extensions" / "giftsense-theme"
LANGS = ["es", "fr", "de", "it", "pt"]


@pytest.mark.parametrize("locale,name", [
    (None, None), ("en", None), ("en-GB", None), ("fr", "French"), ("de-AT", "German"), ("pt-BR", "Brazilian Portuguese"),
    ("sw", "the language with code 'sw'"), ("en_US", None), ("<script>", None),
])
def test_language_name(locale, name):
    assert languages.language_name(locale) == name


def test_rerank_prompt_asks_for_the_shoppers_language():
    base = dict(recipient="friend", occasion="birthday", budget_band="25_50")
    assert "Write each reason in French" in _prompt(Intake(**base, locale="fr"), [])
    assert "Write each reason" not in _prompt(Intake(**base, locale="en-US"), [])


@pytest.mark.asyncio
async def test_note_is_written_in_the_shoppers_language():
    from app.llm import LLMResponse
    from app.services.gifting.notes import draft_note
    seen = {}

    async def chat(**kw):
        seen.update(kw)
        return LLMResponse('{"note": "Joyeux anniversaire !"}', 10, 5, "gpt-6-luna")
    async def moderate(text):
        return False
    await draft_note(recipient="friend", occasion="birthday", tone="warm", max_chars=200, banned_words=[],
                     model="gpt-6-luna", chat_fn=chat, moderate_fn=moderate, locale="fr")
    assert "Write the note in French." in seen["prompt"]


# ── Widget translations ─────────────────────────────────────────────────────

def ui_keys() -> set[str]:
    s = (EXT / "assets" / "giftsense-ui.js").read_text()
    unesc = lambda x: re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), x).replace("\\'", "'")
    keys = {unesc(m.group(2)) for m in re.finditer(r"tr\((['\"])((?:\\.|(?!\1).)*)\1", s)}
    o = intake_options()
    for group in ("recipients", "occasions", "vibes", "age_bands", "budgets"):
        keys |= {x["label"] for x in o[group]}
    for q in vocab.REFINE_QUESTIONS:
        keys |= {q["question"], *(x["label"] for x in q["options"])}
    return keys | set(NOTE_TONES.values())


def translations(lang: str) -> dict:
    text = (EXT / "assets" / f"giftsense-i18n-{lang}.js").read_text()
    return json.loads(text[text.index("{"):text.rindex("}") + 1])


@pytest.mark.parametrize("lang", LANGS)
def test_every_widget_string_is_translated(lang):
    """A new tr('…') string or intake label without translations fails here."""
    d = translations(lang)
    missing = ui_keys() - set(d)
    assert not missing, f"{lang} is missing: {sorted(missing)}"
    for k, v in d.items():
        assert set(re.findall(r"\{\w+\}", k)) == set(re.findall(r"\{\w+\}", v)), (lang, k)
        assert v.strip(), (lang, k)


def test_theme_locales_match_and_every_block_knows_every_language():
    base = json.loads((EXT / "locales" / "en.default.json").read_text())
    flat = lambda d, p="": {f"{p}{k}" for k, v in d.items() for _ in [0] if not isinstance(v, dict)} | \
        {x for k, v in d.items() if isinstance(v, dict) for x in flat(v, f"{p}{k}.")}
    blocks = [(EXT / "blocks" / b).read_text() for b in ("app-embed.liquid", "gift-finder.liquid", "gift-options.liquid")]
    for lang in LANGS:
        assert flat(json.loads((EXT / "locales" / f"{lang}.json").read_text())) == flat(base), lang
        assert all(f"gs_lang == '{lang}'" in b for b in blocks)
    assert sorted(p.stem.removeprefix("giftsense-i18n-") for p in (EXT / "assets").glob("giftsense-i18n-*.js")) == sorted(LANGS)
