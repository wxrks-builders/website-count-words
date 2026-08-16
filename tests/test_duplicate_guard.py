"""Tests for the post-fetch duplicate guard, and for the filter-list split
that keeps the stateful frontier filter out of the sitemap estimate.

The frontier filter (app/url_policy.py) works from URL shape alone, so it can
only ever be a heuristic. This is the layer that makes the word total right
regardless — and the split below is what stops it corrupting the estimate.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(Path(__file__).resolve().parents[1] / ".env"))

from app import crawler  # noqa: E402
from app.crawler import _declared_canonical, _duplicate_of  # noqa: E402
from app.url_policy import FrontierDedupeFilter, canonical_key  # noqa: E402


class FakeJob:
    def __init__(self, counted=()):
        self.counted_keys = {canonical_key(u) for u in counted}
        self.content_hashes: dict[str, str] = {}


class FakeResult:
    def __init__(self, url, html=""):
        self.url = url
        self.html = html


def _canonical_html(href):
    return f'<head><link rel="canonical" href="{href}"/></head>'


class TestDeclaredCanonical:
    def test_reads_either_attribute_order(self):
        url = "https://clay.com/x"
        assert _declared_canonical('<link rel="canonical" href="https://clay.com/y">', url) == "https://clay.com/y"
        assert _declared_canonical("<link href='https://clay.com/y' rel='canonical'>", url) == "https://clay.com/y"

    def test_resolves_a_relative_href(self):
        assert _declared_canonical('<link rel="canonical" href="/y">', "https://clay.com/x/z") == "https://clay.com/y"

    def test_absent_or_unparseable(self):
        assert _declared_canonical("<head></head>", "https://clay.com/x") is None
        assert _declared_canonical(None, "https://clay.com/x") is None
        # A stylesheet link must not be mistaken for a canonical one.
        assert _declared_canonical('<link rel="stylesheet" href="/a.css">', "https://clay.com/x") is None


class TestDuplicateOf:
    def test_declared_canonical_pointing_at_a_counted_page(self):
        """The real signal, verified live against the culprit: a Webflow
        paginated variant declares itself canonical at the un-paginated URL."""
        job = FakeJob(counted=["https://www.clay.com/glossary/site-retargeting"])
        result = FakeResult(
            "https://www.clay.com/glossary/site-retargeting?page-nrpb=2",
            _canonical_html("https://www.clay.com/glossary/site-retargeting"),
        )
        assert _duplicate_of(job, result, "some text") == "https://www.clay.com/glossary/site-retargeting"

    def test_self_referential_canonical_is_not_a_duplicate(self):
        """Nearly every page declares itself canonical — that must never read
        as 'duplicate of itself'."""
        job = FakeJob()
        url = "https://www.clay.com/about"
        assert _duplicate_of(job, FakeResult(url, _canonical_html(url)), "text") is None

    def test_canonical_pointing_somewhere_not_yet_counted_is_kept(self):
        job = FakeJob()
        result = FakeResult("https://clay.com/x?page=2", _canonical_html("https://clay.com/x"))
        assert _duplicate_of(job, result, "text") is None

    def test_identical_text_under_an_unrelated_path(self):
        """What no URL rule could ever catch."""
        job = FakeJob()
        text = "The same body copy, republished."
        assert _duplicate_of(job, FakeResult("https://clay.com/a"), text) is None
        assert _duplicate_of(job, FakeResult("https://clay.com/b"), text) == "https://clay.com/a"

    def test_different_text_is_not_a_duplicate(self):
        job = FakeJob()
        assert _duplicate_of(job, FakeResult("https://clay.com/a"), "one") is None
        assert _duplicate_of(job, FakeResult("https://clay.com/b"), "two") is None

    def test_empty_pages_do_not_collapse_into_each_other(self):
        """Every empty page is 'identical' to every other one, and they're
        already worth zero words — folding them together would just make the
        duplicate count lie."""
        job = FakeJob()
        assert _duplicate_of(job, FakeResult("https://clay.com/a"), "") is None
        assert _duplicate_of(job, FakeResult("https://clay.com/b"), "   \n ") is None


class TestFilterListSplit:
    """_build_estimate_result replays its filters over every URL in the
    sitemap. Handing it the stateful frontier filter would let sitemap URLs
    claim canonical keys the real crawl then gets rejected for, silently
    truncating it — so run_crawl keeps two lists and passes the stateless one.
    """

    def test_terminal_status_is_given_scope_filters_not_the_chain(self):
        source = inspect.getsource(crawler.run_crawl)
        assert "_resolve_terminal_status(job, pause_at_words, url, scope_filters)" in source
        assert "FilterChain(scope_filters + [frontier_filter])" in source
        # The stateful filter reaches the chain by being added at the call
        # above, never by joining the list that gets replayed over the sitemap.
        assert "scope_filters.append(FrontierDedupeFilter" not in source
        assert "scope_filters.append(frontier_filter)" not in source

    def test_sitemap_filtering_over_a_stateless_list_is_repeatable(self):
        """The property the split protects: replaying scope filters over the
        same URLs must give the same answer every time. A frontier filter in
        that list would fail this on the second pass."""
        from app.crawler import SkipDownloadsFilter, TopDomainOnlyFilter

        urls = ["https://clay.com/a", "https://clay.com/a", "https://clay.com/b.pdf"]
        scope_filters = [TopDomainOnlyFilter("clay.com"), SkipDownloadsFilter()]
        first = [all(f.apply(u) for f in scope_filters) for u in urls]
        second = [all(f.apply(u) for f in scope_filters) for u in urls]
        assert first == second == [True, True, False]

        stateful = [FrontierDedupeFilter()]
        assert [all(f.apply(u) for f in stateful) for u in urls] == [True, False, True]
