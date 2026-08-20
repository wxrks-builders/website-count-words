"""Language selection, catalogues, and the formatting that goes with them.

The ten languages are exactly the ones wxrks.com publishes, taken from its own
hreflang tags. It addresses them by short path prefix — /es, /de, /pt — while
its hreflang carries the regional code, and this app matches both halves: get
one of them wrong and the two properties disagree about what a language is,
which is precisely what hreflang exists to prevent.

Translations are not written here. Catalogues ship with English filled and the
rest empty, and come back translated as .po files with no code change — see
locales/README.md.
"""

from __future__ import annotations

import gettext
import os
from pathlib import Path

from babel import Locale
from babel.numbers import format_decimal

# English is the root, so it has no prefix. The order is the order they appear
# in a language picker.
DEFAULT_LANG = "en"
LANGUAGES: tuple[str, ...] = ("en", "ar", "de", "es", "fr", "it", "ja", "ko", "pt", "sv", "zh")

# What each one is called in its own language — the only sensible way to label a
# language picker, since somebody looking for Japanese is not reading English.
LANGUAGE_NAMES = {
    "en": "English", "ar": "العربية", "de": "Deutsch", "es": "Español",
    "fr": "Français", "it": "Italiano", "ja": "日本語", "ko": "한국어",
    "pt": "Português", "sv": "Svenska", "zh": "中文",
}

# The path is short; the hreflang is regional, matching what wxrks.com declares.
# A search engine told "de" and a sibling site told "de-DE" will treat them as
# different targets, so these have to agree across both properties.
HREFLANG = {
    "en": "en", "ar": "ar", "de": "de-DE", "es": "es", "fr": "fr-FR",
    "it": "it", "ja": "ja", "ko": "ko", "pt": "pt-BR", "sv": "sv", "zh": "zh",
}

# Written right to left, so the whole layout mirrors rather than just the text.
RTL_LANGUAGES = frozenset({"ar"})

# A pseudo-locale for finding strings that were never extracted. Every
# translated string comes back wrapped, so anything still bare on the page is
# hard-coded English somebody missed. Enabled only when PSEUDO_LOCALE is set,
# because it must never be reachable in production.
PSEUDO_LANG = "zz"
PSEUDO_ENABLED = os.environ.get("PSEUDO_LOCALE", "").lower() in ("1", "true", "yes")

LOCALE_DIR = Path(__file__).resolve().parent.parent / "locales"

_translations: dict[str, gettext.NullTranslations] = {}


def N_(message: str) -> str:
    """Marks a string for extraction without translating it.

    Module-level text — the landing copy in app/surfaces.py, the plan features —
    is built at import, long before a request exists to have a language. This
    puts it in the catalogue where it is written, and the template calls _() on
    it at render time, when there is a language to translate into.
    """
    return message


def selectable_languages() -> tuple[str, ...]:
    return LANGUAGES + (PSEUDO_LANG,) if PSEUDO_ENABLED else LANGUAGES


def is_language(code: str) -> bool:
    return code in selectable_languages()


def translations_for(lang: str) -> gettext.NullTranslations:
    """The catalogue for a language, loaded once.

    Falls back to untranslated English rather than raising: a language whose .mo
    hasn't been compiled yet should show English, not a 500. That is also what
    makes it safe to add a language to LANGUAGES before its catalogue exists.
    """
    if lang not in _translations:
        try:
            _translations[lang] = gettext.translation(
                "messages", localedir=str(LOCALE_DIR), languages=[lang]
            )
        except OSError:
            _translations[lang] = gettext.NullTranslations()
    return _translations[lang]


def gettext_for(lang: str):
    """The `_` a template or route uses. The pseudo-locale wraps instead of
    translating, so untranslated text is visible rather than merely absent."""
    if lang == PSEUDO_LANG:
        return lambda message: f"⟦{message}⟧"
    return translations_for(lang).gettext


def split_path(path: str) -> tuple[str, str]:
    """("/es/runs") -> ("es", "/runs"). Anything else keeps its path and gets
    the default, so an unknown first segment stays a normal 404 rather than
    being swallowed as a language nobody offers."""
    parts = path.split("/", 2)
    if len(parts) > 1 and is_language(parts[1]) and parts[1] != DEFAULT_LANG:
        return parts[1], "/" + (parts[2] if len(parts) > 2 else "")
    return DEFAULT_LANG, path


def localized_path(path: str, lang: str) -> str:
    """The prefixed form of an internal path.

    English is the root, so it never gains a prefix. Static files never do
    either — they aren't translated and prefixing them would make every language
    miss the browser cache for the same asset.
    """
    if lang == DEFAULT_LANG or not path.startswith("/") or path.startswith("/static/"):
        return path
    return f"/{lang}{path}"


def locale_for(lang: str) -> Locale:
    """Babel's locale for formatting. The pseudo-locale formats as English —
    it exists to find missing strings, not to invent a number format."""
    try:
        return Locale.parse(HREFLANG.get(lang, lang), sep="-")
    except Exception:
        return Locale.parse("en")


def format_number(value: int | float, lang: str) -> str:
    """662431 -> "662,431" in English, "662.431" in German, "662 431" in French.

    This app's whole output is a number, so leaving it in US grouping for every
    language would be the product getting it visibly wrong in ten places at once.
    """
    return format_decimal(value, locale=locale_for(lang))
