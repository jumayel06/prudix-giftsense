"""Which language the AI writes in (docs/TECHNICAL_PLAN.md §9: reasons and
notes follow the storefront's `request.locale`). The widget sends the locale
(e.g. "fr", "pt-BR"); English needs no instruction. Any language works for
the model, so unknown codes are passed through by code; the widget's own text
is translated separately (theme extension assets/giftsense-i18n-*.js)."""
import re

NAMES = {
    "es": "Spanish", "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese", "nl": "Dutch",
    "sv": "Swedish", "da": "Danish", "nb": "Norwegian", "no": "Norwegian", "fi": "Finnish", "pl": "Polish",
    "cs": "Czech", "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "tr": "Turkish", "ar": "Arabic",
    "he": "Hebrew", "el": "Greek", "ro": "Romanian", "hu": "Hungarian", "uk": "Ukrainian", "id": "Indonesian",
    "th": "Thai", "vi": "Vietnamese", "hi": "Hindi",
}
LOCALE_RE = r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$"


def language_name(locale: str | None) -> str | None:
    """None for English (or nothing); otherwise the language to write in."""
    if not locale or not re.fullmatch(LOCALE_RE, locale):
        return None
    base = locale.split("-")[0].lower()
    if base == "en":
        return None
    name = NAMES.get(base, f"the language with code '{base}'")
    if locale.lower() == "pt-br":
        name = "Brazilian Portuguese"
    return name
