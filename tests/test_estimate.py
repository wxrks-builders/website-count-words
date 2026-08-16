"""Tests for how the pre-crawl estimate projects a duration.

The estimate is shown on the one screen where somebody decides whether the
crawl is worth waiting for, and it extrapolates from a sample of ~15 pages to a
site of tens of thousands. That multiplier is around 1,000x, so anything
included in the sample's elapsed time that isn't actually per-page cost gets
magnified into hours that don't exist.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(Path(__file__).resolve().parents[1] / ".env"))

from app import crawler  # noqa: E402
from app.models import PageResult  # noqa: E402


class FakeJob:
    """A job that fetched `pages` pages, having spent `startup` seconds getting
    the browser up before the first of them."""

    def __init__(self, pages=15, words=50_319, startup=6.0, crawl=12.0, concurrency=4):
        now = datetime.now(timezone.utc)
        self.started_at = (now - timedelta(seconds=startup + crawl)).isoformat()
        self.crawling_since = now - timedelta(seconds=crawl)
        self.pages = {f"https://x.com/{i}": PageResult(url=f"https://x.com/{i}") for i in range(pages)}
        self.total_words = words
        self.resume_state = None
        self.cms_match_counts = {"Webflow": 4}
        self.concurrency = concurrency


def build(job, sitemap_count=16_708):
    async def fake_sitemap(url, filters):
        return sitemap_count

    original = crawler._discover_sitemap_page_count
    crawler._discover_sitemap_page_count = fake_sitemap
    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(crawler._build_estimate_result(job, "https://x.com", []))
        finally:
            loop.close()
    finally:
        crawler._discover_sitemap_page_count = original


class TestStartupIsNotMultiplied:
    def test_browser_startup_does_not_become_hours(self):
        """The bug this replaces: elapsed time was measured from job creation,
        so Chromium launching and the <html lang> probe were treated as per-page
        cost and scaled by the whole site. On a 15-page sample of a 16,708-page
        site that is a 1,114x multiplier — six seconds became nearly two hours.
        """
        slow_start = build(FakeJob(startup=6.0, crawl=12.0))
        no_start = build(FakeJob(startup=0.0, crawl=12.0))
        # Startup is charged once, so the two projections differ by roughly the
        # startup itself — not by startup x (16708/15).
        difference = slow_start["estimated_duration_seconds"] - no_start["estimated_duration_seconds"]
        assert difference <= 30, f"startup leaked into the per-page rate: {difference}s"

    def test_the_rate_reflects_fetching_not_the_whole_wall_clock(self):
        job = FakeJob(pages=15, startup=6.0, crawl=12.0)
        result = build(job)
        # 15 pages in 12s of fetching is 75/min. Measured over 18s it would be 50.
        assert result["pages_per_minute"] == 75.0

    def test_the_projection_follows_the_rate_and_nothing_else(self):
        """The property that makes this a fix rather than a thumb on the scale:
        two samples that fetched at the same pages/second project the same
        duration, however long the browser took to start in each."""
        fast_start = build(FakeJob(pages=15, startup=1.0, crawl=12.0))
        slow_start = build(FakeJob(pages=1500, startup=20.0, crawl=1200.0))
        assert fast_start["pages_per_minute"] == slow_start["pages_per_minute"]
        drift = abs(fast_start["estimated_duration_seconds"] - slow_start["estimated_duration_seconds"])
        assert drift <= 30, "the projection is still picking up startup cost"


class TestProComparison:
    def test_a_free_crawl_is_shown_what_pro_would_take(self):
        from app import plans

        result = build(FakeJob(concurrency=plans.CONCURRENCY_FREE))
        assert result["estimated_duration_seconds_pro"] is not None
        assert result["estimated_duration_seconds_pro"] < result["estimated_duration_seconds"]

    def test_a_pro_crawl_is_not_advertised_to_itself(self):
        from app import plans

        result = build(FakeJob(concurrency=plans.CONCURRENCY_PRO))
        assert result["estimated_duration_seconds_pro"] is None

    def test_the_pro_figure_uses_the_measured_ratio(self):
        from app import plans

        result = build(FakeJob(concurrency=plans.CONCURRENCY_FREE))
        ratio = result["estimated_duration_seconds"] / result["estimated_duration_seconds_pro"]
        assert abs(ratio - plans.speedup_over(plans.CONCURRENCY_FREE)) < 0.05
