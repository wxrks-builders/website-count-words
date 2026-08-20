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

import pytest  # noqa: E402

from app import i18n  # noqa: E402


@pytest.fixture()
def i18n_client(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "i.db"))
    monkeypatch.setenv("MARKDOWN_DIR", str(tmp_path / "markdown"))
    import importlib

    import app.db as db
    import app.main as main
    importlib.reload(db)
    importlib.reload(main)
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        client.headers["host"] = "wordcounter.wxrks.app"
        yield client


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


class TestTheSwitcher:
    """Routing without a picker means typing URLs, which is not a feature."""

    def _links(self, html):
        import re

        return {n.strip(): h for h, _, n in re.findall(
            r'<a[^>]*href="([^"]*)"[^>]*hreflang="([^"]+)"[^>]*>\s*<span>([^<]+)</span>', html)}

    def test_it_keeps_you_on_the_page_you_are_on(self, i18n_client):
        """Landing somewhere else for choosing a language loses what you were
        reading, which is a small betrayal at exactly the wrong moment."""
        links = self._links(i18n_client.get("/de/login").text)
        assert links["Français"] == "/fr/login"
        assert links["日本語"] == "/ja/login"
        assert links["English"] == "/login", "English is the root, so it has no prefix"

    def test_every_language_is_offered_and_named_in_itself(self, i18n_client):
        links = self._links(i18n_client.get("/").text)
        assert len(links) == len(i18n.LANGUAGES)
        assert "العربية" in links and "日本語" in links and "Deutsch" in links

    def test_the_current_one_is_marked(self, i18n_client):
        assert 'aria-current="true"' in i18n_client.get("/de/login").text

    def test_signed_out_visitors_get_one(self, i18n_client):
        """The public pages are where translation earns its traffic, and the
        same control serves them — not a second, different one."""
        for path in ("/", "/login"):
            html = i18n_client.get(path).text
            assert 'class="lang-menu"' in html, path
            assert "initUserMenu" in html, f"{path} — nothing would open it"

    def test_it_stands_alone_rather_than_living_in_the_account_menu(self, i18n_client):
        """Its own control, so the current language is legible without opening
        anything — which is the whole reason for separating it."""
        html = i18n_client.get("/de/login").text
        assert ">DE<" in html, "the trigger names the current language"
        assert 'data-menu-button' in html

    def test_the_trigger_names_the_current_language(self, i18n_client):
        import re

        for path, expected in (("/", "EN"), ("/de/login", "DE"), ("/ar/login", "AR")):
            html = i18n_client.get(path).text
            assert re.search(r'lang-code">([^<]+)', html).group(1) == expected, path


class TestEmailsFollowTheirReader:
    def test_the_language_is_remembered_on_the_account(self):
        """Emails are sent from background tasks with no request to read a
        prefix from, so browsing is the only moment the choice is observable."""
        from app.models import User

        assert User(id=1, google_sub="s", email="a@b.c", name="A").lang == "en"

    def test_the_crawl_email_uses_the_owners_language(self):
        import inspect

        from app import crawler

        assert "lang=user.lang," in inspect.getsource(crawler.run_crawl)

    def test_the_share_email_uses_the_senders(self):
        """Whoever receives it has no account to hold a preference, and the
        person choosing to share is the one whose words introduce it."""
        import inspect

        from app import main

        assert "lang=request.state.lang," in inspect.getsource(main.email_share)

    def test_email_numbers_are_grouped_for_the_reader_too(self):
        from app.notifications import _num

        assert _num(662431, "en") == "662,431"
        assert _num(662431, "de") == "662.431"


class TestNothingLeaksMarkup:
    def test_the_home_pitch_bolds_the_number_rather_than_printing_tags(self, i18n_client, monkeypatch):
        """newstyle gettext escapes interpolated values, so markup has to be
        marked safe on the parameter — |safe on the result arrives too late and
        blesses text that already reads '&lt;strong&gt;'."""
        import asyncio

        import app.db as db
        import app.main as main
        from app.auth import get_current_user, require_user, require_user_api
        from app.models import PageResult, User

        u = User(id=1, google_sub="a", email="o@x.c", name="R")
        for dep in (require_user, require_user_api, get_current_user):
            main.app.dependency_overrides[dep] = lambda: u

        def run(coro):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        run(db.get_or_create_user("a", "o@x.c", "R", None))
        run(db.save_run(run_id="r", source_url="https://wxrks.com", user_id=1,
                        status="completed", total_words=1_076_780,
                        pages=[PageResult(url="https://wxrks.com/a", word_count=1076780)],
                        limit_reached=False))
        html = i18n_client.get("/").text
        main.app.dependency_overrides.clear()
        assert "&lt;strong&gt;" not in html
        assert "<strong>" in html


class TestAssetsBustTheCache:
    def test_static_references_carry_a_version(self, i18n_client):
        """Browsers cache /static and nothing ever told them a deploy happened,
        so every deploy shipped new HTML against week-old CSS and JS — which is
        how a new menu renders as an unstyled button that won't open."""
        html = i18n_client.get("/login").text
        assert "style.css?v=" in html
        assert "app.js?v=" in html

    def test_the_version_changes_when_the_files_do(self, tmp_path, monkeypatch):
        from app import templates as t

        monkeypatch.setattr(t, "_STATIC_DIR", tmp_path)
        (tmp_path / "style.css").write_text("a{}")
        (tmp_path / "app.js").write_text("//1")
        first = t._asset_version()
        (tmp_path / "app.js").write_text("//2")
        assert t._asset_version() != first
