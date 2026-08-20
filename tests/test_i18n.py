"""Tests for the ten languages.

The catalogues themselves are empty until wxrks fills them, so nothing here
asserts a translation. What it asserts is the plumbing: that a language is
reachable, that it doesn't change which route answers, that numbers follow the
reader rather than the developer, and that Arabic mirrors.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(Path(__file__).resolve().parents[1] / ".env"))

from app import i18n  # noqa: E402


class TestTheLanguagesAreTheOnesWxrksPublishes:
    def test_ten_plus_english(self):
        assert set(i18n.LANGUAGES) == {"en", "ar", "de", "es", "fr", "it", "ja", "ko", "pt", "sv", "zh"}

    def test_every_language_has_a_name_in_its_own_language(self):
        """A picker labelled in English is no use to someone looking for 日本語."""
        for code in i18n.LANGUAGES:
            assert i18n.LANGUAGE_NAMES.get(code), code

    def test_hreflang_carries_the_regional_codes(self):
        """wxrks.com declares de-DE, fr-FR and pt-BR against short paths. A
        search engine told 'de' by one property and 'de-DE' by another treats
        them as different targets, which is what hreflang exists to prevent."""
        assert i18n.HREFLANG["de"] == "de-DE"
        assert i18n.HREFLANG["fr"] == "fr-FR"
        assert i18n.HREFLANG["pt"] == "pt-BR"

    def test_every_language_has_a_catalogue_on_disk(self):
        for code in i18n.LANGUAGES:
            if code == i18n.DEFAULT_LANG:
                continue
            po = i18n.LOCALE_DIR / code / "LC_MESSAGES" / "messages.po"
            assert po.exists(), f"{code} has no catalogue"


class TestPathRouting:
    def test_a_language_prefix_is_stripped(self):
        assert i18n.split_path("/es/runs") == ("es", "/runs")
        assert i18n.split_path("/zh/crawl/abc123") == ("zh", "/crawl/abc123")

    def test_english_lives_at_the_root(self):
        """/en/ is not a path — it would be a second URL for the same page,
        which is the duplicate-content problem hreflang is meant to avoid."""
        assert i18n.split_path("/runs") == ("en", "/runs")
        assert i18n.split_path("/en/runs") == ("en", "/en/runs")

    def test_an_unknown_prefix_is_left_alone(self):
        """So it 404s as a missing page rather than being swallowed as a
        language nobody offers."""
        assert i18n.split_path("/nope/runs") == ("en", "/nope/runs")
        assert i18n.split_path("/crawl/es") == ("en", "/crawl/es")

    def test_building_a_link_round_trips(self):
        for code in i18n.LANGUAGES:
            built = i18n.localized_path("/runs", code)
            assert i18n.split_path(built) == (code, "/runs"), code

    def test_static_assets_are_never_prefixed(self):
        """Each language would otherwise miss the browser cache for a file they
        all share."""
        for code in i18n.LANGUAGES:
            assert i18n.localized_path("/static/app.js", code) == "/static/app.js"


class TestNumbersFollowTheReader:
    def test_the_product_is_a_number_so_it_is_formatted_per_locale(self):
        assert i18n.format_number(662431, "en") == "662,431"
        assert i18n.format_number(662431, "de") == "662.431"
        assert i18n.format_number(662431, "pt") == "662.431"

    def test_french_and_swedish_group_with_spaces(self):
        for code in ("fr", "sv"):
            grouped = i18n.format_number(662431, code)
            assert grouped.replace(" ", " ").replace("\xa0", " ") == "662 431", code

    def test_every_language_formats_without_raising(self):
        for code in i18n.LANGUAGES:
            assert i18n.format_number(1234567, code)


class TestArabicMirrors:
    def test_only_arabic_is_right_to_left(self):
        assert i18n.RTL_LANGUAGES == {"ar"}

    def test_the_stylesheet_has_no_physical_directions_left(self):
        """Logical properties are what let one stylesheet serve both
        directions; a physical one silently doesn't mirror."""
        import re

        css = (Path(__file__).resolve().parents[1] / "app/static/style.css").read_text()
        offenders = re.findall(
            r"(?:margin|padding|border)-(?:left|right)\s*:|text-align:\s*(?:left|right)\b|^\s*(?:left|right)\s*:",
            css, re.M,
        )
        assert offenders == [], f"{len(offenders)} physical properties would not mirror"


class TestTheCatalogueIsComplete:
    def test_the_javascript_strings_are_in_the_same_catalogue(self):
        """A second front-end catalogue would mean two extractions and two
        places to send a translator."""
        from app import js_strings

        pot = (i18n.LOCALE_DIR / "messages.pot").read_text()
        for value in js_strings.catalogue("en").values():
            first_line = value.split("\n")[0].replace('"', '\\"')
            assert first_line[:40] in pot, f"missing from catalogue: {value[:40]}"

    def test_the_landing_copy_is_extracted(self):
        """It is built at import, before any request has a language, so it is
        marked with N_() where it is written and translated at render."""
        from app import surfaces

        pot = (i18n.LOCALE_DIR / "messages.pot").read_text()
        assert surfaces.COUNTER.headline[:40] in pot
        assert surfaces.COUNTER.points[0][0][:30] in pot


class TestPseudoLocale:
    def test_it_wraps_instead_of_translating(self):
        """Its whole job is to make an unextracted string visible: anything
        still bare on a /zz/ page is hard-coded English."""
        assert i18n.gettext_for(i18n.PSEUDO_LANG)("Total words") == "⟦Total words⟧"

    def test_it_is_off_unless_asked_for(self):
        assert i18n.PSEUDO_LANG not in i18n.LANGUAGES
