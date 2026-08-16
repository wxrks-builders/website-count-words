"""URL identity and exclusion rules for a single crawl's frontier.

Deliberately separate from db.normalize_url(), which keys *runs* by their
source URL and has to stay stable forever — change it and every cached run
stops matching. This module keys *pages within one crawl*, and is free to be
much more aggressive.

Why this exists at all: crawl4ai does dedupe the frontier, but far more weakly
than it looks. normalize_url_for_deep_crawl() only strips the fragment,
lowercases the host, and drops five tracking params (utm_source, utm_medium,
utm_campaign, ref, fbclid). Everything else — including every pagination
parameter — survives into BFSDeepCrawlStrategy's `visited` set, so each
combination reads as a brand-new page.

That is fine until a page embeds more than one paginated list. A Webflow
glossary page with four of them produced, for one single page:

    /glossary/site-retargeting?645ae0eb_page=3&e85c56e5_page=2&page-nrpb=2&page-nwzk=2

and 1,138 more like it. One real clay.com crawl came back with 59,860 URLs and
253.8M words for a site of roughly 11,300 pages and 15M words. With max_pages
unlimited and max_depth at 1000 there was nothing to bound it.
"""

from __future__ import annotations

import collections
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from crawl4ai.deep_crawling.filters import URLFilter

# A URL may carry at most this many pagination parameters. One is the whole
# point: the crawler must still walk a single paginator, or the articles behind
# "page 2, 3, 4..." of a blog index never get discovered. It's *combinations*
# that explode — with four independent paginators the frontier is the cartesian
# product of their page counts.
MAX_PAGINATION_PARAMS_PER_URL = 1

# Generic backstop for query schemes the pagination pattern below doesn't
# recognize: a single path may contribute at most this many distinct
# *shapes* of query — that is, distinct sets of parameter NAMES, ignoring their
# values.
#
# Counting names rather than whole query strings matters, and the first version
# of this got it wrong by counting strings. A site that identifies pages by a
# query parameter — clay.com/jobs?ashby_jid=<id> is 71 distinct job postings,
# app.clay.com/signup?priceId=<id> another 362 — produces one shape and many
# values, and capping the values threw away every page past the fifth. What
# actually explodes is *combinations of parameters*, and that shows up as
# distinct shapes: {a_page}, {a_page,b_page}, {a_page,b_page,c_page}...
MAX_QUERY_SHAPES_PER_PATH = 5

# Runaway guard, and deliberately generous. Shape-counting above is blind to a
# scheme that puts something unique in the *value* on every link — a session id,
# a cache buster — which would otherwise mint new URLs forever, each one
# fetched before the post-fetch duplicate guard could notice it's the same page.
# Set far above any real listing (clay.com's largest is 362 signup variants) so
# it only ever catches genuine runaway, not a site with a lot of products.
MAX_QUERY_VALUES_PER_PATH = 500


# Pagination parameter names, across the shapes seen in the wild:
#   page, p                     generic / WordPress
#   page-nwzk, page-nrpb        Webflow, one per collection list on the page
#   645ae0eb_page               Webflow, hashed variant of the same thing
#   offset, start, limit        API-ish listing params
_PAGINATION_PARAM_RE = re.compile(
    r"^(?:p|page|paged|offset|start|limit|page[-_][0-9a-z]+|[0-9a-z]+_page)$",
    re.IGNORECASE,
)

# Parameters that never change what a page says, only who gets credit for the
# visit. Dropping them from the identity key collapses e.g. the two
# app.clay.com/signup rows that differed solely by ?source=nav vs
# ?source=website. Prefix families (utm_*, _hs*) are handled separately below.
_TRACKING_PARAMS = {
    "gclid", "gbraid", "wbraid", "fbclid", "msclkid", "yclid", "ttclid",
    "igshid", "twclid", "li_fat_id", "dclid",
    "mc_cid", "mc_eid", "dub_id", "_ga", "_gl", "gad_source",
    "ref", "referrer", "referer", "source", "src", "via", "su",
    # Post-auth destinations. These decide where you land *after* the page,
    # never what the page itself says, so app.clay.com/signup?redirect_to=<144
    # different pages> is one page, fetched once.
    "redirect_to", "redirect", "redirect_uri", "return_to", "returnurl",
    "return_url", "next", "continue", "callback", "destination",
}
_TRACKING_PREFIXES = ("utm_", "_hs", "pk_", "mtm_", "at_")


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TRACKING_PARAMS or lowered.startswith(_TRACKING_PREFIXES)


def is_pagination_param(name: str) -> bool:
    return bool(_PAGINATION_PARAM_RE.match(name))


def pagination_param_count(url: str) -> int:
    query = urlsplit(url).query
    if not query:
        return 0
    return sum(1 for name, _ in parse_qsl(query, keep_blank_values=True) if is_pagination_param(name))


def canonical_key(url: str) -> str:
    """The identity of a page for frontier purposes.

    Collapses the differences that never mean "a different page": scheme, a
    leading www., host case, a trailing slash, tracking parameters, and
    parameter order. Pagination parameters are deliberately *kept* — page 2 of
    a listing really is different content, and it's the frontier rules below,
    not this key, that stop them multiplying.
    """
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    # Scheme is pinned rather than preserved: a site linking to itself over
    # both http and https is the same site, and the crawler follows whichever
    # redirect it's given anyway.
    path = parts.path.rstrip("/")
    params = sorted(
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(name)
    )
    return urlunsplit(("https", host, path, urlencode(params), ""))


def _path_key(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host, parts.path.rstrip("/")


def _query_shape(url: str) -> frozenset[str] | None:
    """The set of parameter names this URL uses, ignoring their values, or None
    when the shape shouldn't be counted at all.

    None for two cases. A query-less URL has no shape to track. And a query made
    up only of pagination parameters is already governed by
    MAX_PAGINATION_PARAMS_PER_URL — counting it here as well would cap a single
    paginator at a handful of pages, which is the exact opposite of that rule's
    intent: "?page=6" onwards would vanish, and every article linked only from
    those pages with it.
    """
    parts = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    names = {name for name, _ in parts if not _is_tracking_param(name)}
    if not names or all(is_pagination_param(n) for n in names):
        return None
    return frozenset(names)


class FrontierDedupeFilter(URLFilter):
    """Keeps one crawl's frontier from multiplying the same page.

    Slots into the same FilterChain as the domain/download/language filters,
    but unlike them it is stateful, which puts two constraints on it.

    First, it must be idempotent under re-testing. BFSDeepCrawlStrategy's
    link_discovery() checks `visited` *before* the filter chain and adds to
    `visited` immediately after a URL passes, so an admitted URL runs through
    here exactly once — but a rejected one is re-tested from every other page
    that links to it. Every rule below is a stable function of already-claimed
    state, so those re-tests keep returning the same answer.

    Second, it must never be handed URLs that aren't real crawl candidates —
    see the scope_filters/chain_filters split in app.crawler, which keeps this
    out of the sitemap estimate pass.
    """

    def __init__(self, seed_urls: list[str] | None = None):
        super().__init__(name="FrontierDedupeFilter")
        self._claimed_keys: set[str] = set()
        self._shapes: dict[tuple[str, str], set[frozenset[str]]] = {}
        self._value_counts: collections.Counter = collections.Counter()
        self.rejected_duplicate = 0
        self.rejected_pagination = 0
        self.rejected_shape_cap = 0
        self.rejected_runaway = 0
        # A resumed crawl rebuilds its claimed keys from the URLs it already
        # visited. Without this, every canonical whose first variant was
        # crawled before the pause is unclaimed on resume, so a second variant
        # of it sails through — one duplicate per page, per resume.
        for url in seed_urls or []:
            self._claim(url)

    def _claim(self, url: str) -> None:
        self._claimed_keys.add(canonical_key(url))
        shape = _query_shape(url)
        if shape is not None:
            self._shapes.setdefault(_path_key(url), set()).add(shape)
            self._value_counts[_path_key(url)] += 1

    def apply(self, url: str) -> bool:
        if pagination_param_count(url) > MAX_PAGINATION_PARAMS_PER_URL:
            self.rejected_pagination += 1
            self._update_stats(False)
            return False

        key = canonical_key(url)
        if key in self._claimed_keys:
            self.rejected_duplicate += 1
            self._update_stats(False)
            return False

        shape = _query_shape(url)
        if shape is not None:
            shapes = self._shapes.setdefault(_path_key(url), set())
            if shape not in shapes and len(shapes) >= MAX_QUERY_SHAPES_PER_PATH:
                self.rejected_shape_cap += 1
                self._update_stats(False)
                return False
            path_key = _path_key(url)
            if self._value_counts[path_key] >= MAX_QUERY_VALUES_PER_PATH:
                self.rejected_runaway += 1
                self._update_stats(False)
                return False
            shapes.add(shape)
            self._value_counts[path_key] += 1

        self._claimed_keys.add(key)
        self._update_stats(True)
        return True

    @property
    def rejected_total(self) -> int:
        return (self.rejected_duplicate + self.rejected_pagination
                + self.rejected_shape_cap + self.rejected_runaway)


def parse_exclusions(text: str | None) -> list[str]:
    """Parses the exclusions field's comma-separated text into a list of
    entries, e.g. "staging, web-staging, /careers" -> ["staging",
    "web-staging", "/careers"]. Mirrors parse_languages() in app.crawler."""
    if not text:
        return []
    return [part.strip().lower() for part in text.split(",") if part.strip()]


def exclusion_kind(entry: str) -> str:
    """"folder" or "subdomain" for one entry, by the same leading-"/" rule
    ExclusionFilter applies. Exposed so the UI labels an entry exactly the way
    the crawler will treat it, rather than describing the rule in prose next to
    a box and hoping the two stay in step."""
    return "folder" if entry.strip().startswith("/") else "subdomain"


def describe_exclusions(text: str | None) -> list[tuple[str, str]]:
    """[(entry, kind), ...] for display."""
    return [(e, exclusion_kind(e)) for e in parse_exclusions(text)]


class ExclusionFilter(URLFilter):
    """Drops subdomains and folders the person running the crawl asked to leave
    out — the escape hatch for "whole domain, but not the staging mirror".

    An entry starting with "/" excludes a path prefix; anything else excludes a
    host, matching either the full hostname or a leading label, so "staging"
    covers staging.example.com and web-staging.example.com alike.
    """

    def __init__(self, entries: list[str]):
        super().__init__(name="ExclusionFilter")
        self._paths = [e.rstrip("/") for e in entries if e.startswith("/")]
        self._hosts = [e for e in entries if not e.startswith("/")]

    def _excluded(self, url: str) -> bool:
        parts = urlsplit(url)
        host = parts.netloc.split(":")[0].lower()
        # Dropped for the same reason canonical_key() drops it: "www." is never
        # what distinguishes one host from another, so excluding
        # web-staging.example.com has to catch www.web-staging.example.com too.
        if host.startswith("www."):
            host = host[4:]
        for entry in self._hosts:
            if host == entry or host.startswith(f"{entry}."):
                return True
            # A bare label matches any subdomain whose first label contains it,
            # so "staging" catches web-staging.example.com too.
            first_label = host.split(".")[0]
            if entry in first_label.split("-") or first_label == entry:
                return True
        path = parts.path.rstrip("/").lower()
        for entry in self._paths:
            if path == entry or path.startswith(f"{entry}/"):
                return True
        return False

    def apply(self, url: str) -> bool:
        passed = not self._excluded(url)
        self._update_stats(passed)
        return passed
