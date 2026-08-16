"""Tests for when the two banners appear, and when they stay quiet.

The staying-quiet half is the point. Both promotions are conditional on being
true — a banner that always shows is one people stop seeing — so most of what
follows checks that they don't fire.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(Path(__file__).resolve().parents[1] / ".env"))

from app import plans, promos, surfaces  # noqa: E402
from app.models import RunRecord, User  # noqa: E402
from app.promos import pro_upsell, wxrks_pitch  # noqa: E402

SLOW = 3 * 3600  # a crawl somebody went away and came back from


def run_record(**kw):
    body = dict(
        id="r", source_url="https://www.clay.com", user_id=1,
        created_at="2026-08-16T10:00:00+00:00", status="completed",
        total_words=662_000, page_count=100, limit_reached=False,
        crawl_concurrency=plans.CONCURRENCY_FREE, duration_seconds=SLOW, pages=[],
    )
    body.update(kw)
    return RunRecord(**body)


def user(pro=False):
    return User(id=1, google_sub="s", email="a@b.c", name="A",
                plan="pro" if pro else "free", plan_status="active" if pro else None)


class TestProUpsellStaysQuiet:
    def test_when_there_is_nothing_to_sell(self):
        assert pro_upsell(run_record(), user(), billing_enabled=False) is None

    def test_for_someone_already_paying(self):
        assert pro_upsell(run_record(), user(pro=True), True) is None

    def test_for_a_visitor_who_cannot_buy(self):
        """A shared report has no signed-in user to sell to."""
        assert pro_upsell(run_record(), None, True) is None

    def test_for_a_crawl_that_has_not_finished(self):
        for status in ("crawling", "paused", "cancelled", "failed"):
            assert pro_upsell(run_record(status=status), user(), True) is None, status

    def test_for_a_crawl_nobody_waited_on(self):
        assert pro_upsell(run_record(duration_seconds=90), user(), True) is None

    def test_for_a_run_whose_duration_was_never_recorded(self):
        """0 is "unknown", not "instant" — every run saved before durations
        existed has it, and claiming a time we don't have is worse than silence.
        """
        assert pro_upsell(run_record(duration_seconds=0), user(), True) is None

    def test_for_a_crawl_that_already_ran_at_pro_speed(self):
        assert pro_upsell(run_record(crawl_concurrency=plans.CONCURRENCY_PRO), user(), True) is None

    def test_when_the_saving_rounds_away_to_nothing(self):
        """Just over the time threshold but with a saving too small to be worth
        a sentence — there is no argument to make, so it doesn't make one."""
        result = pro_upsell(
            run_record(duration_seconds=promos.PRO_UPSELL_MIN_SECONDS,
                       crawl_concurrency=plans.CONCURRENCY_PRO - 1),
            user(), True,
        )
        assert result is None


class TestProUpsellSpeaks:
    def test_after_a_slow_crawl(self):
        result = pro_upsell(run_record(), user(), True)
        assert result is not None
        assert result["took_seconds"] == SLOW
        assert result["pro_seconds"] < SLOW

    def test_the_quoted_time_uses_the_same_ratio_as_the_pricing_page(self):
        """If these drifted, the banner would promise something the pricing page
        doesn't, or the reverse."""
        result = pro_upsell(run_record(), user(), True)
        expected = round(SLOW / plans.speedup_over(plans.CONCURRENCY_FREE))
        assert result["pro_seconds"] == expected
        assert result["speedup"] == plans.advertised_speedup()


class TestWxrksPitch:
    def test_shown_on_a_shared_counter_report(self):
        result = wxrks_pitch(run_record(), surfaces.COUNTER, "shared")
        assert result is not None
        assert result["domain"] == "clay.com"
        assert result["total_words"] == 662_000

    def test_not_shown_to_the_owner_of_the_report(self):
        """The signed-in owner is a customer, not a lead — and they came to
        count words, not to be sold to on their own page."""
        assert wxrks_pitch(run_record(), surfaces.COUNTER, "past") is None
        assert wxrks_pitch(run_record(), surfaces.COUNTER, "live") is None

    def test_not_shown_on_the_markdown_surface(self):
        """Someone extracting Markdown is building a retrieval pipeline, not
        shopping for translation."""
        assert wxrks_pitch(run_record(), surfaces.MARKDOWN, "shared") is None

    def test_not_shown_for_a_site_too_small_to_be_a_project(self):
        assert wxrks_pitch(run_record(total_words=200), surfaces.COUNTER, "shared") is None

    def test_the_link_carries_the_run_and_is_encoded(self):
        result = wxrks_pitch(run_record(), surfaces.COUNTER, "shared")
        query = parse_qs(urlsplit(result["url"]).query)
        assert query["utm_source"] == ["wordcounter"]
        assert query["utm_medium"] == ["shared_report"]
        assert query["words"] == ["662000"]
        assert query["site"] == ["clay.com"]

    def test_www_is_stripped_from_the_domain_shown(self):
        assert wxrks_pitch(run_record(source_url="https://www.example.com"),
                           surfaces.COUNTER, "shared")["domain"] == "example.com"
