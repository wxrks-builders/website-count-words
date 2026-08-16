from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import psutil
from crawl4ai import (
    AsyncUrlSeeder,
    AsyncWebCrawler,
    BrowserConfig,
    CrawlerRunConfig,
    DefaultMarkdownGenerator,
    PruningContentFilter,
    SeedingConfig,
)
from crawl4ai.deep_crawling import BFSDeepCrawlStrategy
from crawl4ai.deep_crawling.filters import DomainFilter, FilterChain, URLFilter
from crawl4ai.utils import get_base_domain

from app import db, markdown_store, surfaces
from app.job_store import dequeue_next, get_job, list_active_jobs
from app.models import PageResult
from app.notifications import send_crawl_notification
from app.plans import CONCURRENCY_FLOOR, active_page_load, resolve_concurrency, speedup_over
from app.url_policy import ExclusionFilter, FrontierDedupeFilter, canonical_key, parse_exclusions
from app.word_count import count_words

logger = logging.getLogger(__name__)


def _markdown_text(result) -> str:
    markdown = getattr(result, "markdown", None)
    if markdown is None:
        return ""
    if isinstance(markdown, str):
        return markdown
    # fit_markdown (main-content only, via the PruningContentFilter below) is
    # far closer to visible article text than raw_markdown's full-page dump.
    fit = getattr(markdown, "fit_markdown", None)
    if fit and not fit.startswith("Error generating fit markdown"):
        return fit
    return getattr(markdown, "raw_markdown", None) or ""


def _markdown_for_storage(result) -> str:
    """The Markdown to save for a page — main content only.

    Deliberately separate from _markdown_text() above, which feeds the word
    count. That one looks like it prefers fit_markdown but never does:
    result.markdown is a str subclass, so its isinstance() check always wins and
    every count to date is raw_markdown-based. Fixing it there would change what
    every future crawl counts and break comparability with saved runs, so this
    reads fit_markdown directly and leaves counting exactly as it was.
    """
    markdown = getattr(result, "markdown", None)
    if markdown is None:
        return ""
    fit = getattr(markdown, "fit_markdown", None)
    if fit and not fit.startswith("Error generating fit markdown"):
        return fit
    return getattr(markdown, "raw_markdown", None) or str(markdown)


_IMAGE_MARKDOWN_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_MARKDOWN_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_LEADING_MARKUP_RE = re.compile(r"^\s*(#{1,6}|[-*+]|\d+\.|>)\s+", re.MULTILINE)


def clean_markdown_for_counting(text: str) -> str:
    """Strips markdown syntax a browser text-selection would never include: image markup, link URLs, heading/bullet markers."""
    text = _IMAGE_MARKDOWN_RE.sub("", text)
    text = _LINK_MARKDOWN_RE.sub(r"\1", text)
    text = _LEADING_MARKUP_RE.sub("", text)
    return text


_LOGIN_KEYWORDS = (
    "log in",
    "login",
    "log-in",
    "sign in",
    "signin",
    "sign-in",
    "please log in",
    "authentication required",
    "you must be logged in",
)


class TopDomainOnlyFilter(URLFilter):
    """Restricts a crawl to the exact registrable domain (optionally with a
    'www.' prefix), rejecting any other subdomain. Crawl4AI's own DomainFilter
    can't express this — its allowed_domains matching treats any subdomain as
    a match by design, which is the opposite of what "top domain only" needs.
    """

    def __init__(self, base_domain: str):
        super().__init__(name="TopDomainOnlyFilter")
        self._base_domain = base_domain

    def apply(self, url: str) -> bool:
        host = urlparse(url).netloc.split(":")[0].lower()
        if host.startswith("www."):
            host = host[4:]
        passed = host == self._base_domain
        self._update_stats(passed)
        return passed


# Extensions a browser treats as a download rather than something to render
# — navigating to one of these with Playwright fails with a "Download is
# starting" error instead of ever producing page text, so they're skipped
# before ever being fetched rather than counted as crawl failures.
_DOWNLOAD_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".mp3", ".mp4", ".mov", ".avi", ".wmv", ".wav",
    ".exe", ".dmg", ".apk",
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp",
}


class SkipDownloadsFilter(URLFilter):
    def __init__(self):
        super().__init__(name="SkipDownloadsFilter")

    def apply(self, url: str) -> bool:
        path = urlparse(url).path.lower()
        passed = not any(path.endswith(ext) for ext in _DOWNLOAD_EXTENSIONS)
        self._update_stats(passed)
        return passed


# ISO 639-1 two-letter language codes — a stable, unchanging standard, used
# to recognize language-prefixed path segments (e.g. /en/, /fr/) without
# needing an external lookup.
_ISO_639_1_CODES = {
    "aa", "ab", "ae", "af", "ak", "am", "an", "ar", "as", "av", "ay", "az",
    "ba", "be", "bg", "bh", "bi", "bm", "bn", "bo", "br", "bs",
    "ca", "ce", "ch", "co", "cr", "cs", "cu", "cv", "cy",
    "da", "de", "dv", "dz",
    "ee", "el", "en", "eo", "es", "et", "eu",
    "fa", "ff", "fi", "fj", "fo", "fr", "fy",
    "ga", "gd", "gl", "gn", "gu", "gv",
    "ha", "he", "hi", "ho", "hr", "ht", "hu", "hy", "hz",
    "ia", "id", "ie", "ig", "ii", "ik", "io", "is", "it", "iu",
    "ja", "jv",
    "ka", "kg", "ki", "kj", "kk", "kl", "km", "kn", "ko", "kr", "ks", "ku", "kv", "kw", "ky",
    "la", "lb", "lg", "li", "ln", "lo", "lt", "lu", "lv",
    "mg", "mh", "mi", "mk", "ml", "mn", "mr", "ms", "mt", "my",
    "na", "nb", "nd", "ne", "ng", "nl", "nn", "no", "nr", "nv", "ny",
    "oc", "oj", "om", "or", "os",
    "pa", "pi", "pl", "ps", "pt",
    "qu",
    "rm", "rn", "ro", "ru", "rw",
    "sa", "sc", "sd", "se", "sg", "si", "sk", "sl", "sm", "sn", "so", "sq", "sr", "ss", "st", "su", "sv", "sw",
    "ta", "te", "tg", "th", "ti", "tk", "tl", "tn", "to", "tr", "ts", "tt", "tw", "ty",
    "ug", "uk", "ur", "uz",
    "ve", "vi", "vo",
    "wa", "wo",
    "xh",
    "yi", "yo",
    "za", "zh", "zu",
}


def _lang_code(segment: str) -> str:
    """Normalizes a locale-shaped segment to its base language code,
    e.g. "pt-BR" / "pt_BR" -> "pt".

    Strips first: the field asks for "en, pt, es", and without this the space
    rode along into the keep set as " pt", which then matched no path segment
    at all — so every language after the first was silently dropped."""
    return segment.strip().lower().split("-")[0].split("_")[0]


def _looks_like_language_segment(segment: str) -> bool:
    code = _lang_code(segment)
    return len(code) == 2 and code in _ISO_639_1_CODES


def parse_languages(language: str | None) -> list[str]:
    """Parses the language field's comma-separated text into a list of
    normalized codes, e.g. "en, pt, es, fr" -> ["en", "pt", "es", "fr"]."""
    if not language:
        return []
    return [_lang_code(part) for part in language.split(",") if part.strip()]


class LanguageFilter(URLFilter):
    """Restricts a crawl to one or more languages, for sites that publish the
    same content under multiple /xx/ path prefixes. A URL is rejected only if
    its first path segment looks like a language code that ISN'T in the kept
    set — a segment that doesn't look like a language code at all is always
    allowed through, so this works whether or not the site's primary/default
    language has its own prefix.

    Heuristic, not exact: a two-letter path segment that happens to coincide
    with an ISO 639-1 code but isn't actually a language marker (e.g. a
    country section, an unrelated product code) could be misclassified —
    same trade-off as this app's other content heuristics (login-wall and
    anti-bot detection).
    """

    def __init__(self, keep_languages: list[str]):
        super().__init__(name="LanguageFilter")
        self._keep = {_lang_code(l) for l in keep_languages if l.strip()}

    def apply(self, url: str) -> bool:
        segments = urlparse(url).path.split("/")
        first = next((s for s in segments if s), "")
        if not _looks_like_language_segment(first):
            passed = True
        else:
            passed = _lang_code(first) in self._keep
        self._update_stats(passed)
        return passed


_HTML_LANG_RE = re.compile(r'<html[^>]+\blang=["\']([a-zA-Z0-9-]+)["\']', re.IGNORECASE)


def _detect_page_language(html: str | None) -> str | None:
    """Reads the page's own declared language (the same <html lang="..">
    signal search engines and browsers rely on) so a blank language field
    can mean "restrict to whatever language this page is in" instead of
    "no restriction at all"."""
    match = _HTML_LANG_RE.search(html or "")
    if not match:
        return None
    code = _lang_code(match.group(1))
    return code if len(code) == 2 and code in _ISO_639_1_CODES else None


# <link rel="canonical" href="..."> in either attribute order — the site's own
# statement of "this URL and that URL are the same page". Used by the duplicate
# guard below, which is why it tolerates the messier real-world orderings
# rather than assuming rel comes first.
_CANONICAL_LINK_RE = re.compile(
    r'<link[^>]*\brel=["\']canonical["\'][^>]*\bhref=["\']([^"\']+)["\']'
    r'|<link[^>]*\bhref=["\']([^"\']+)["\'][^>]*\brel=["\']canonical["\']',
    re.IGNORECASE,
)


def _declared_canonical(html: str | None, page_url: str) -> str | None:
    match = _CANONICAL_LINK_RE.search(html or "")
    if not match:
        return None
    href = match.group(1) or match.group(2)
    if not href:
        return None
    # Resolved against the page, since a canonical is allowed to be relative.
    return urljoin(page_url, href.strip())


def _duplicate_of(job, result, text: str) -> str | None:
    """Which already-counted page this one is a copy of, if any.

    The frontier filter (app/url_policy.py) stops the bulk of this before a
    fetch ever happens, but it works from URL shape alone and so can only ever
    be a heuristic. This is the backstop that makes the *word total* right
    regardless, using two signals the page itself provides:

      - Its declared <link rel="canonical">. Verified against the real culprit:
        /glossary/site-retargeting?page-nrpb=2&645ae0eb_page=3 declares itself
        canonical at ...?645ae0eb_page=3, so the site is telling us outright.
      - A hash of the exact text count_words() is about to count. Catches the
        same content republished under a genuinely unrelated path, which no
        URL rule could ever see.

    Costs ~16 bytes plus a key per page, which is nothing against the RSS
    ceiling that already cancels crawls.
    """
    canonical = _declared_canonical(getattr(result, "html", None), result.url)
    if canonical:
        declared_key = canonical_key(canonical)
        if declared_key != canonical_key(result.url) and declared_key in job.counted_keys:
            return canonical

    if not text.strip():
        # Empty pages are all "identical" to each other and would otherwise
        # collapse into one — they're already worth 0 words, so let them be.
        return None
    digest = hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=16).hexdigest()
    seen_at = job.content_hashes.get(digest)
    if seen_at is not None:
        return seen_at
    job.content_hashes[digest] = result.url
    return None


PAUSE_AT_WORDS = 50_000
# Raised from 3 alongside the per-crawl concurrency work: total page load is now
# bounded by plans.PAGE_BUDGET rather than by this number, so this is about how
# many people can be served at once, not how hard the box gets pushed. The extra
# slot is what keeps a paid crawl from waiting behind three free ones.
MAX_CONCURRENT_CRAWLS = int(os.environ.get("MAX_CONCURRENT_CRAWLS", "4"))
_CHECKPOINT_EVERY = 20
# How long a crawl can go with zero new page results before it's treated as
# hung (a wedged fetch/browser context, not just a slow site) and cancelled
# automatically — see _stall_watchdog. Checked periodically rather than
# exactly on the deadline; _STALL_CHECK_INTERVAL_SECONDS is the poll period.
STALL_TIMEOUT_SECONDS = 300
_STALL_CHECK_INTERVAL_SECONDS = 30

_process = psutil.Process()
_MEMORY_LIMIT_BYTES = int(os.environ.get("MEMORY_LIMIT_MB", "3200")) * 1024 * 1024


def _memory_exceeded() -> bool:
    # This process's own RSS — unlike crawl4ai's built-in memory-adaptive
    # dispatcher (psutil.virtual_memory(), host-wide), this reflects what
    # actually counts against a container's cgroup memory limit.
    return _process.memory_info().rss >= _MEMORY_LIMIT_BYTES


def _capture_markdown(job, result) -> None:
    """Saves one page's Markdown, if this run asked for it.

    Every failure mode here is non-fatal by design: the crawl has already done
    the expensive part, and losing a word count because a disk filled up would
    be a far worse outcome than losing the Markdown. Any problem stops capture
    for the rest of the run and leaves the crawl completely untouched.
    """
    if not job.capture_markdown or job.markdown_state != "capturing":
        return
    try:
        if job.markdown_bytes >= markdown_store.MAX_RUN_BYTES:
            job.markdown_state = "stopped_run_cap"
            return
        text = _markdown_for_storage(result)
        if not text:
            return
        job.markdown_bytes += markdown_store.write(job.id, result.url, text)
        job.markdown_pages += 1
    except Exception:
        logger.exception("Markdown capture failed for %s — continuing the crawl without it", result.url)
        job.markdown_state = "error"


def _check_markdown_budget(job) -> None:
    """Called at checkpoints rather than per page — disk_usage and a full-tree
    size are far too expensive to run every result."""
    if job.markdown_state != "capturing":
        return
    try:
        if markdown_store.disk_is_tight():
            job.markdown_state = "stopped_disk"
        elif markdown_store.total_bytes() >= markdown_store.MAX_TOTAL_BYTES:
            job.markdown_state = "stopped_global_cap"
    except Exception:
        logger.exception("Could not check the Markdown budget — stopping capture for this run")
        job.markdown_state = "error"


def _recovered_resume_state(job) -> dict | None:
    """Puts back the URLs crawl4ai's own resume snapshot drops.

    BFSDeepCrawlStrategy marks an entire level visited before it fetches any of
    it, but the snapshot's "pending" only ever carries the NEXT level. Stop
    mid-level — which is exactly what the estimate pause does — and every URL in
    the current level that hadn't been fetched yet is both marked visited and
    missing from pending, so a resume never looks at it again.

    On a well interlinked site the next level is usually empty as well (every
    link already visited), so pending comes back as [], the resumed crawl starts
    with an empty frontier, and it finishes instantly having crawled nothing.
    That's the bug where "Proceed with crawl" appeared to complete immediately.

    Anything visited that produced no result is therefore restored to pending.
    """
    state = job.resume_state
    if not state:
        return None
    pending = list(state.get("pending") or [])
    queued = {item.get("url") for item in pending}
    fetched = set(job.pages) | set(job.login_blocked)
    recovered = [
        {"url": url, "parent_url": None}
        for url in state.get("visited") or []
        if url not in fetched and url not in queued
    ]
    if not recovered:
        return state
    return {**state, "pending": pending + recovered}


def _elapsed_seconds(job) -> int:
    """How long this run has been going, wall-clock. Written at every save so a
    crawl that is interrupted still leaves a usable duration behind rather than
    a zero — app/promos.py treats 0 as "unknown" and stays quiet."""
    try:
        started = datetime.fromisoformat(job.started_at)
    except (TypeError, ValueError):
        return 0
    return max(0, round((datetime.now(timezone.utc) - started).total_seconds()))


async def _checkpoint(job, languages: list[str]) -> None:
    # Persists progress mid-crawl (not just at the end) so a server crash
    # can auto-resume from here instead of losing everything — see
    # app.main's startup scan of db.get_crawling_runs().
    _check_markdown_budget(job)
    await db.save_run(
        run_id=job.id,
        source_url=job.source_url,
        user_id=job.user_id,
        status="crawling",
        total_words=job.total_words,
        pages=list(job.pages.values()),
        limit_reached=False,
        login_blocked_count=len(job.login_blocked) + job.restored_login_blocked_count,
        duplicate_count=job.duplicate_count,
        domain_scope=job.domain_scope,
        exclusions=job.exclusions,
        crawl_concurrency=job.concurrency,
        duration_seconds=_elapsed_seconds(job),
        language=",".join(languages) if languages else None,
        language_auto_detected=job.detected_language is not None,
        resume_state=_recovered_resume_state(job),
        capture_markdown=job.capture_markdown,
        markdown_pages=job.markdown_pages,
        markdown_bytes=job.markdown_bytes,
        markdown_state=job.markdown_state,
        surface=job.surface,
    )


async def _discover_sitemap_page_count(url: str, filters: list[URLFilter]) -> int | None:
    """Best-effort: how many pages does this site's sitemap list, after
    applying the same domain/language filters the real crawl would use?
    Returns None if no sitemap could be found — not every site has one, and
    that's not an error, just a missing signal."""
    hostname = urlparse(url).netloc
    try:
        async with AsyncUrlSeeder() as seeder:
            config = SeedingConfig(source="sitemap", extract_head=False, live_check=False)
            discovered = await seeder.urls(hostname, config)
    except Exception:
        return None
    if not discovered:
        return None
    urls = [item["url"] for item in discovered if item.get("url")]
    if filters:
        urls = [u for u in urls if all(f.apply(u) for f in filters)]
    return len(urls) if urls else None


# Regex signatures matched against already-fetched page HTML/headers — no
# extra network requests. Each pattern is weighted: 2 for a strong, highly
# specific signal (a generator meta tag, the WordPress REST API discovery
# header) where a single hit is trustworthy on its own, 1 for a weaker
# asset-path substring that could coincidentally appear once, requiring a
# second hit (on any page) before it's trusted — see _resolve_detected_cms.
# CMS platforms with a well-known, standard sitemap convention (as opposed
# to Contentful, a headless CMS whose sitemap behavior depends entirely on
# whatever frontend framework sits on top).
_CMS_SIGNATURES: dict[str, list[tuple[re.Pattern, int]]] = {
    "WordPress": [
        (re.compile(r'name=["\']generator["\'][^>]*content=["\']WordPress', re.IGNORECASE), 2),
        (re.compile(r'/wp-content/'), 1),
        (re.compile(r'/wp-includes/'), 1),
    ],
    "Webflow": [
        (re.compile(r'data-wf-page='), 2),
        (re.compile(r'name=["\']generator["\'][^>]*content=["\']Webflow', re.IGNORECASE), 2),
        (re.compile(r'assets\.website-files\.com'), 1),
    ],
    "Shopify": [
        (re.compile(r'cdn\.shopify\.com'), 1),
        (re.compile(r'Shopify\.theme'), 2),
    ],
    "Squarespace": [
        (re.compile(r'static1\.squarespace\.com'), 1),
        (re.compile(r'name=["\']generator["\'][^>]*content=["\']Squarespace', re.IGNORECASE), 2),
    ],
    "Wix": [
        (re.compile(r'static\.wixstatic\.com'), 1),
        (re.compile(r'name=["\']generator["\'][^>]*content=["\']Wix', re.IGNORECASE), 2),
    ],
    "Ghost": [
        (re.compile(r'name=["\']generator["\'][^>]*content=["\']Ghost', re.IGNORECASE), 2),
    ],
    "Drupal": [
        (re.compile(r'Drupal\.settings'), 2),
        (re.compile(r'/sites/default/files/'), 1),
    ],
    "Contentful": [
        (re.compile(r'ctfassets\.net'), 1),
        (re.compile(r'cdn\.contentful\.com'), 1),
    ],
}
_WORDPRESS_REST_LINK_RE = re.compile(r'rel="https://api\.w\.org/"')
# CMS platforms whose sitemap conventions are standard/well-known enough that
# detecting them corroborates a found sitemap's completeness. Contentful is
# deliberately excluded — see the note above.
_SITEMAP_CONVENTION_CMS = {"WordPress", "Webflow", "Shopify", "Squarespace", "Wix", "Ghost", "Drupal"}


def _detect_cms_signals(result) -> dict[str, int]:
    """Which CMS signatures matched this already-fetched page, as {name:
    weight}. Takes the strongest pattern's weight per name if more than one
    of that CMS's patterns matched the same page, rather than summing them —
    a single page shouldn't count twice toward the same CMS."""
    html = result.html or ""
    matches: dict[str, int] = {}
    for name, patterns in _CMS_SIGNATURES.items():
        weight = max((w for p, w in patterns if p.search(html)), default=0)
        if weight:
            matches[name] = weight
    headers = getattr(result, "response_headers", None) or {}
    link_header = headers.get("link", "") or headers.get("Link", "")
    if _WORDPRESS_REST_LINK_RE.search(link_header):
        matches["WordPress"] = max(matches.get("WordPress", 0), 2)
    return matches


def _resolve_detected_cms(cms_match_counts: dict[str, int]) -> str | None:
    """A running sum of per-page weights (see _detect_cms_signals) — a
    single strong-signal page (weight 2) already clears the bar, while
    weak asset-path-only signals need to show up on a second page before
    being trusted, since a lone substring hit could be coincidental."""
    if not cms_match_counts:
        return None
    best_name, best_count = max(cms_match_counts.items(), key=lambda kv: kv[1])
    return best_name if best_count >= 2 else None


async def _build_estimate_result(job, url: str, filters: list[URLFilter]) -> dict:
    pages_fetched = len(job.pages)
    avg_words_per_page = job.total_words / pages_fetched if pages_fetched else 0
    discovered_total = len(job.resume_state["visited"]) if job.resume_state else pages_fetched
    sitemap_count = await _discover_sitemap_page_count(url, filters)
    # The sitemap is usually the more complete number this early — the BFS
    # traversal may not have reached deep enough yet to discover everything
    # itself within the word budget.
    total_pages_estimate = max(sitemap_count or 0, discovered_total)
    sitemap_found = sitemap_count is not None
    detected_cms = _resolve_detected_cms(job.cms_match_counts)
    if not sitemap_found:
        confidence = "low"
    elif detected_cms in _SITEMAP_CONVENTION_CMS:
        confidence = "high"
    else:
        confidence = "medium"

    now = datetime.now(timezone.utc)
    started_at = datetime.fromisoformat(job.started_at)
    elapsed_seconds = max((now - started_at).total_seconds(), 0.01)

    # Rate is measured over the fetching window only. Projecting from total
    # elapsed time instead multiplies the one-time startup — Chromium launching,
    # crawl4ai initialising, the language probe — by however many pages the site
    # has. On a 15-page sample of a 16,708-page site that is a factor of 1,114:
    # six seconds of browser startup became nearly two hours of predicted crawl.
    crawl_seconds = max((now - (job.crawling_since or started_at)).total_seconds(), 0.01)
    startup_seconds = max(elapsed_seconds - crawl_seconds, 0)
    crawl_minutes = crawl_seconds / 60
    words_per_minute = job.total_words / crawl_minutes
    pages_per_minute = pages_fetched / crawl_minutes
    # Startup is added back once, not once per page.
    estimated_duration_seconds = (
        startup_seconds + crawl_seconds * (total_pages_estimate / pages_fetched)
        if pages_fetched
        else elapsed_seconds
    )
    # +1 for this job itself — by this point job.status has already flipped
    # to "paused" (set by the caller just before this runs), so it's no
    # longer in list_active_jobs()'s "starting"/"crawling" set, even though
    # it was actively occupying a slot for basically this entire run.
    concurrent_crawls = len(list_active_jobs()) + 1

    # What the same crawl would take on Pro. Linear in page concurrency, which
    # holds while this app is the bottleneck and stops holding once the site
    # being crawled is — so the UI states it as an approximation. None when the
    # crawl is already running at Pro speed, which is how the panel knows not to
    # advertise anything.
    speedup = speedup_over(job.concurrency)
    pro_duration_seconds = round(estimated_duration_seconds / speedup) if speedup > 1 else None

    return {
        "pages_fetched": pages_fetched,
        "discovered_total": discovered_total,
        "sitemap_count": sitemap_count,
        "sitemap_found": sitemap_found,
        "detected_cms": detected_cms,
        "confidence": confidence,
        "total_pages_estimate": total_pages_estimate,
        "avg_words_per_page": round(avg_words_per_page),
        "estimated_total_words": round(avg_words_per_page * total_pages_estimate),
        "elapsed_seconds": round(elapsed_seconds),
        "words_per_minute": round(words_per_minute),
        "pages_per_minute": round(pages_per_minute, 1),
        "estimated_duration_seconds": round(estimated_duration_seconds),
        "estimated_duration_seconds_pro": pro_duration_seconds,
        "concurrency": job.concurrency,
        "concurrent_crawls": concurrent_crawls,
    }


def estimate_result_from_snapshot(snapshot: dict) -> dict | None:
    """Rebuilds the same shape _build_estimate_result produces, from a saved
    estimate_history row — used by app.main's lifespan to restore a paused
    job's estimate display after a restart, since job.estimate_result
    otherwise only ever lived on the original in-memory Job. avg_words_per_page
    isn't itself a stored column, so it's back-derived from the two totals
    that are. Returns None for a pre-timing-columns snapshot (an older row
    from before those columns existed) rather than a dict full of Nones the
    UI isn't prepared to render — same as if there were no snapshot at all."""
    if snapshot.get("words_per_minute") is None or snapshot.get("estimated_duration_seconds") is None:
        return None
    total_pages_estimate = snapshot["estimated_total_pages"]
    estimated_total_words = snapshot["estimated_total_words"]
    concurrency = snapshot.get("crawl_concurrency") or 0
    avg_words_per_page = round(estimated_total_words / total_pages_estimate) if total_pages_estimate else 0
    return {
        "pages_fetched": snapshot["pages_fetched"],
        "discovered_total": snapshot["discovered_total"],
        "sitemap_count": snapshot["sitemap_count"],
        "sitemap_found": bool(snapshot["sitemap_found"]),
        "detected_cms": snapshot["detected_cms"],
        "confidence": snapshot["confidence"],
        "total_pages_estimate": total_pages_estimate,
        "avg_words_per_page": avg_words_per_page,
        "estimated_total_words": estimated_total_words,
        "elapsed_seconds": snapshot["elapsed_seconds"],
        "words_per_minute": snapshot["words_per_minute"],
        "pages_per_minute": snapshot["pages_per_minute"],
        "estimated_duration_seconds": snapshot["estimated_duration_seconds"],
        # Not a stored column — recomputed from the same ratio the live panel
        # uses, so a restored estimate still shows the Pro comparison.
        "estimated_duration_seconds_pro": (
            round(snapshot["estimated_duration_seconds"] / speedup_over(concurrency))
            if concurrency and speedup_over(concurrency) > 1
            else None
        ),
        "concurrency": concurrency,
        "concurrent_crawls": snapshot["concurrent_crawls"],
    }


def _clean_error_message(message: str) -> str:
    """crawl4ai sometimes formats an internal exception with a full
    traceback-style dump attached (file paths, line numbers, a "Code
    context:" listing) — strip that down to just the human-readable reason
    before it ever reaches the UI."""
    if not message:
        return "Failed to fetch page"
    message = message.split("Code context:")[0].strip()
    if "Download is starting" in message:
        return "This link points to a downloadable file, not a webpage"
    if len(message) > 200:
        message = message[:200].rstrip() + "…"
    return message


def _is_login_wall(result) -> bool:
    """Heuristic: crawl4ai has no dedicated "requires login" signal, so this
    combines the strongest hints available — an outright auth status code,
    a redirect to a login-shaped URL, or login wording in the page title."""
    if getattr(result, "status_code", None) in (401, 403):
        return True

    for candidate_url in (result.url, getattr(result, "redirected_url", None)):
        if candidate_url and ("login" in candidate_url.lower() or "signin" in candidate_url.lower()):
            return True

    title = ""
    if result.metadata:
        title = (result.metadata.get("title") or "").lower()
    if any(kw in title for kw in _LOGIN_KEYWORDS):
        return True

    return False


async def _resolve_terminal_status(job, pause_at_words: int | None, url: str, filters: list[URLFilter]) -> None:
    """Figures out *why* the crawl loop stopped and sets job.status (and any
    accompanying fields) accordingly. Shared between the normal
    loop-exhausted-naturally path and the CancelledError handler in
    run_crawl, since a direct self-cancel (see the main loop) can now reach
    this for the memory/pause cases too, not just a natural level-boundary
    stop."""
    if job.cancel_requested:
        job.status = "cancelled"
        # The stall watchdog already claimed this one if it was the cause.
        job.stop_kind = job.stop_kind or "user_cancelled"
    elif _memory_exceeded():
        job.status = "cancelled"
        job.stop_kind = "memory"
        job.stopped_reason = (
            "This crawl was stopped automatically — it was using too much "
            "memory to continue safely on this server."
        )
    elif pause_at_words is not None and job.total_words >= pause_at_words:
        # Hit the pause threshold with the frontier still non-empty — this
        # is a genuine pause, not a finish. If the site instead exhausted
        # its own links before ever reaching the threshold, this branch is
        # skipped entirely and it's just a normal completion, exact rather
        # than estimated.
        job.status = "paused"
        job.stop_kind = "paused"
        # Repaired before anything reads it: /resume takes resume_state straight
        # off the job, and the final save_run below persists whatever is here.
        job.resume_state = _recovered_resume_state(job)
        job.estimate_result = await _build_estimate_result(job, url, filters)
        await db.save_estimate_snapshot(job.id, job.source_url, job.estimate_result)
    else:
        job.status = "completed"
        job.stop_kind = "completed"


async def _maybe_start_next_queued() -> None:
    """Called whenever a crawl finishes (however it ended) — if there's
    room and something waiting, starts the next queued job the exact same
    way a fresh POST /crawl would. The one hook point that covers every way
    a slot can free up (completed, failed, cancelled, or paused)."""
    if len(list_active_jobs()) >= MAX_CONCURRENT_CRAWLS:
        return
    next_id = dequeue_next()
    if next_id is None:
        return
    job = get_job(next_id)
    if job is None:
        return
    language = job.language_setting or job.detected_language
    # Re-resolved rather than reused: this job was sized when it was queued,
    # against a box that was full. It's starting now precisely because that
    # changed, so it should get whatever the budget can afford at this moment.
    job.concurrency = resolve_concurrency(await db.get_user(job.user_id), active_page_load(list_active_jobs()))
    job.status = "starting"
    # Exclusions aren't passed: run_crawl reads them straight off the job,
    # which is where POST /crawl already put them — same as capture_markdown.
    job.task = asyncio.create_task(
        run_crawl(job.id, job.source_url, job.max_pages, job.domain_scope, language, pause_at_words=PAUSE_AT_WORDS)
    )


async def _stall_watchdog(job) -> None:
    """Runs alongside run_crawl's main loop, independent of it — the
    existing self-cancel checks (_should_cancel, the direct job.task.cancel()
    after each yielded result) only ever get a chance to run BETWEEN page
    results, so a genuinely hung fetch or wedged browser context (no more
    results ever yielded) would otherwise leave a job "crawling" forever,
    permanently squatting on one of MAX_CONCURRENT_CRAWLS's slots. This task
    polls job.last_progress_at independently of whatever the main loop is
    doing, so it can still act even while that loop is stuck."""
    while True:
        await asyncio.sleep(_STALL_CHECK_INTERVAL_SECONDS)
        if job.status not in ("starting", "crawling"):
            return
        idle_seconds = (datetime.now(timezone.utc) - job.last_progress_at).total_seconds()
        if idle_seconds >= STALL_TIMEOUT_SECONDS:
            job.stopped_reason = (
                f"This crawl stalled — no page was fetched for over "
                f"{STALL_TIMEOUT_SECONDS // 60} minutes — and was stopped automatically."
            )
            # Set before request_cancel(), because that sets cancel_requested
            # and from there this is indistinguishable from someone clicking
            # Cancel — which is exactly the confusion this tag exists to end.
            job.stop_kind = "stalled"
            job.request_cancel()
            return


async def run_crawl(
    job_id: str,
    url: str,
    max_pages: int,
    domain_scope: str = "all",
    language: str | None = None,
    pause_at_words: int | None = None,
    resume_state: dict | None = None,
    capture_markdown: bool = False,
    exclusions: str | None = None,
) -> None:
    job = get_job(job_id)
    if job is None:
        return

    # Resolved the same way capture_markdown is just below: the caller's value
    # wins when given (a resume can tighten the list mid-crawl), otherwise
    # whatever the job already carries from POST /crawl or a restore.
    if exclusions is not None:
        job.exclusions = exclusions

    # Every normal path sets this before getting here (POST /crawl, the queue,
    # a restore). This covers the ones that don't: a run saved before the
    # column existed, or a resume of one.
    if not job.concurrency:
        job.concurrency = resolve_concurrency(
            await db.get_user(job.user_id), active_page_load(list_active_jobs())
        )

    # Resolved once here rather than per page: a run that starts with the disk
    # already tight never begins capturing, instead of writing a handful of
    # pages and stopping at the first checkpoint.
    job.capture_markdown = capture_markdown or job.capture_markdown
    if job.capture_markdown and job.markdown_state in ("off", "capturing"):
        job.markdown_state = "stopped_disk" if markdown_store.disk_is_tight() else "capturing" 

    job.status = "crawling"
    job.domain_scope = domain_scope
    job.language_setting = language
    job.last_progress_at = datetime.now(timezone.utc)
    watchdog_task = asyncio.create_task(_stall_watchdog(job))

    # Crawls are always unlimited now, so the depth cap just needs to be high
    # enough not to cut off a legitimately deep site.
    max_depth = 1000

    # By default crawl4ai treats any subdomain of the same registrable domain
    # (e.g. docs.example.com and www.example.com) as "internal", so the whole
    # domain gets crawled — that's what most people want ("all"). "subdomain_only"
    # locks to the exact starting host via crawl4ai's DomainFilter (whose
    # allowed_domains matching treats subdomains as a match, which is exactly
    # what pins it to that host and everything beneath it). "top_domain_only"
    # is the opposite restriction — root domain, but excluding other
    # subdomains — which needs the custom TopDomainOnlyFilter above.
    start_base_domain = get_base_domain(url)

    domain_filters: list[URLFilter] = []
    if domain_scope == "subdomain_only":
        hostname = urlparse(url).netloc
        domain_filters.append(DomainFilter(allowed_domains=[hostname]))
    elif domain_scope == "top_domain_only":
        domain_filters.append(TopDomainOnlyFilter(get_base_domain(url)))
    else:
        # "all" (whole domain incl. subdomains) still needs an explicit
        # filter here, not just crawl4ai's own include_external=False below.
        # That flag recomputes "internal" per page from wherever the browser
        # actually landed, not the original seed — so a page that redirects
        # off-domain (e.g. a marriott.com page landing on whotels.com) gets
        # its own links treated as "internal" relative to the NEW domain,
        # cascading the crawl into a site this scope was never meant to
        # include. BFSDeepCrawlStrategy.can_process_url() applies this
        # filter_chain to every candidate URL regardless of that internal/
        # external classification, so it's the one place that reliably stops
        # the cascade even though the one straddling redirect page itself
        # can't be caught before it's fetched (see _post_fetch_scope_violation
        # for that).
        domain_filters.append(DomainFilter(allowed_domains=[start_base_domain]))

    # Deliberately part of domain_filters rather than a separate list: an
    # excluded host must be rejected by _post_fetch_scope_violation too, or a
    # same-domain link that redirects onto the staging mirror still gets
    # counted despite having been excluded.
    exclusion_entries = parse_exclusions(job.exclusions)
    if exclusion_entries:
        domain_filters.append(ExclusionFilter(exclusion_entries))

    def _post_fetch_scope_violation(result) -> bool:
        # crawl4ai's own domain-scope enforcement (include_external,
        # DomainFilter, TopDomainOnlyFilter) only ever checks a link's
        # pre-fetch candidate URL — it's never re-validated against where
        # the fetch actually landed. A same-domain-looking link that
        # server- or JS-redirects off-domain would otherwise be silently
        # scraped and counted as part of this site (result.url stays
        # pinned to the pre-redirect string, so it'd even look on-domain
        # in the report). result.redirected_url does correctly capture the
        # real destination, so use that — or plain result.url if there was
        # no redirect — for a final check this app controls directly.
        final_url = getattr(result, "redirected_url", None) or result.url
        try:
            if get_base_domain(final_url) != start_base_domain:
                return True
        except Exception:
            return False
        return not all(f.apply(final_url) for f in domain_filters)

    languages = parse_languages(language)

    # Built out here, before the crawler is even opened, because the error
    # paths below hand this list to _resolve_terminal_status — including the
    # CancelledError handler, which can fire while the language probe inside
    # the `async with` is still running.
    scope_filters: list[URLFilter] = list(domain_filters) + [SkipDownloadsFilter()]
    # Bound here rather than inside the `async with` below so the finally block
    # can still log its counters if the crawl dies before the filter is built.
    frontier_filter: FrontierDedupeFilter | None = None

    browser_config = BrowserConfig(
        avoid_css=True,
        avoid_ads=True,
        memory_saving_mode=True,
        max_pages_before_recycle=500,
    )

    try:
        async with AsyncWebCrawler(config=browser_config) as crawler:
            if not languages and resume_state is None:
                # No language typed in, and this isn't a resumed crawl (where
                # the language was already resolved the first time around) —
                # probe the start page's own <html lang> before configuring
                # the crawl, so "blank" means "restrict to whatever language
                # this page is in" rather than "no restriction at all".
                # Best-effort: any failure here just falls through to the old
                # no-restriction behavior.
                try:
                    probe = await crawler.arun(url)
                    detected = _detect_page_language(getattr(probe, "html", None))
                    if detected:
                        languages = [detected]
                        job.detected_language = detected
                except Exception:
                    pass

            job.publish(job.status_payload())

            # Two lists, not one, and the difference matters. scope_filters are
            # all stateless — the same URL always gets the same answer — which
            # is what lets _build_estimate_result replay them over every URL in
            # the sitemap. frontier_filter is stateful: it answers "have I
            # already claimed this page's identity", so replaying it over the
            # sitemap would let sitemap URLs claim keys the real crawl then gets
            # rejected for, quietly truncating the crawl. It goes into the
            # FilterChain and nowhere else.
            if languages:
                scope_filters.append(LanguageFilter(languages))
            frontier_filter = FrontierDedupeFilter(
                # A resumed crawl has to re-claim what it already visited, or
                # one duplicate per page slips through after every pause.
                seed_urls=list((resume_state or {}).get("visited") or [])
            )
            filter_chain = FilterChain(scope_filters + [frontier_filter])

            async def _on_state_change(state: dict) -> None:
                # Lets a paused crawl resume later from exactly this frontier
                # (see BFSDeepCrawlStrategy's resume_state parameter) and, in
                # the meantime, gives an accurate "how many pages have we
                # found links to" count for the pre-crawl estimate.
                job.resume_state = state

            _last_cancel_poll = 0.0

            async def _should_cancel() -> bool:
                # job.cancel_requested only reflects a cancel request that
                # happened to land on THIS process — if the "Cancel crawl"
                # request was handled by a different worker process (this app
                # doesn't share memory across processes), that flag alone
                # would never be seen here. Polling the DB (throttled, since
                # this runs on every should_cancel check) catches it either
                # way within a few seconds instead of not at all.
                nonlocal _last_cancel_poll
                if job.cancel_requested:
                    return True
                now = time.monotonic()
                if now - _last_cancel_poll > 3.0:
                    _last_cancel_poll = now
                    if await db.is_cancel_requested(job.id):
                        job.cancel_requested = True
                        return True
                return _memory_exceeded() or (
                    pause_at_words is not None and job.total_words >= pause_at_words
                )

            strategy = BFSDeepCrawlStrategy(
                max_depth=max_depth,
                max_pages=max_pages,
                include_external=False,
                filter_chain=filter_chain,
                resume_state=resume_state,
                on_state_change=_on_state_change,
                # A should_cancel callback (rather than calling strategy.cancel())
                # because _arun_stream() resets its internal cancel event right as it
                # starts — calling cancel() before that point would be silently wiped
                # out. The callback is re-read live on every check, so it works no
                # matter when request_cancel() sets the flag. Also doubles as the
                # "pause this estimate crawl once it's sampled enough" trigger.
                should_cancel=_should_cancel,
            )
            config = CrawlerRunConfig(
                deep_crawl_strategy=strategy,
                stream=True,
                # The single number that decides how fast this crawl is: how
                # many pages are in flight at once. crawl4ai turns it into
                # MemoryAdaptiveDispatcher(max_session_permit=N). Resolved per
                # crawl from the owner's plan and the shared budget — see
                # app/plans.py.
                semaphore_count=job.concurrency or CONCURRENCY_FLOOR,
                # crawl4ai's default (60s) means a single stuck page can hold
                # up cancellation/pause for that long, since crawl4ai's own
                # cleanup has to wait for an in-flight fetch to finish or
                # error out either way. 30s still comfortably covers
                # legitimately slow pages seen in practice (up to ~26s), while
                # roughly halving that worst-case wait.
                page_timeout=30000,
                markdown_generator=DefaultMarkdownGenerator(content_filter=PruningContentFilter()),
                excluded_tags=["nav", "footer", "aside", "form"],
                word_count_threshold=10,
            )

            # Startup is over; from here on the clock measures fetching only.
            job.crawling_since = datetime.now(timezone.utc)

            async for result in await crawler.arun(url, config=config):
                if result.url in job.pages or result.url in job.login_blocked or result.url in job.duplicates:
                    continue

                if _post_fetch_scope_violation(result):
                    # A redirect took this off the intended domain(s) — see
                    # _post_fetch_scope_violation for why crawl4ai's own
                    # filtering doesn't already catch this. Checked for every
                    # result, not just successful ones — an off-domain URL
                    # that came back blocked/failed (e.g. by the other
                    # domain's own anti-bot detection) is still not real
                    # content for this site, so it's excluded entirely rather
                    # than counted as a page or even shown as a failure.
                    continue

                title = ""
                if result.metadata:
                    title = result.metadata.get("title") or ""

                if _is_login_wall(result):
                    # Hidden from the visible list and excluded from the word
                    # total entirely — it's not real page content, just a wall.
                    page = PageResult(
                        url=result.url,
                        title=title,
                        word_count=0,
                        success=False,
                        login_required=True,
                        error="Requires login",
                    )
                    job.login_blocked[result.url] = page
                    job.last_progress_at = datetime.now(timezone.utc)
                    job.publish(
                        {
                            "type": "login_blocked",
                            "login_blocked_count": len(job.login_blocked),
                        }
                    )
                    continue

                if result.success:
                    text = clean_markdown_for_counting(_markdown_text(result))

                    original_url = _duplicate_of(job, result, text)
                    if original_url is not None:
                        # Same content under a second URL. Recorded, but kept
                        # out of job.pages and job.total_words entirely — the
                        # whole point is that it must not be counted twice —
                        # and its Markdown isn't saved either, since the
                        # identical file is already on disk under the original.
                        job.duplicates[result.url] = PageResult(
                            url=result.url,
                            title=title,
                            word_count=0,
                            success=False,
                            duplicate_of=original_url,
                            error="Duplicate of a page already counted",
                        )
                        job.last_progress_at = datetime.now(timezone.utc)
                        job.publish({"type": "duplicate", "duplicate_count": job.duplicate_count})
                        continue

                    word_count = count_words(text)
                    page = PageResult(url=result.url, title=title, word_count=word_count, success=True)
                    job.total_words += word_count
                    job.counted_keys.add(canonical_key(result.url))
                    _capture_markdown(job, result)
                    for name, weight in _detect_cms_signals(result).items():
                        job.cms_match_counts[name] = job.cms_match_counts.get(name, 0) + weight
                else:
                    raw_error = getattr(result, "error_message", None) or "Failed to fetch page"
                    # Crawl4AI runs its own layered anti-bot detection (Cloudflare/
                    # Akamai/PerimeterX/DataDome challenge pages, 429 rate limits,
                    # structurally-broken "empty shell" responses — see
                    # crawl4ai/antibot_detector.py) and prefixes error_message with
                    # this exact string when it concludes the host blocked us,
                    # rather than the page genuinely not existing/erroring.
                    page = PageResult(
                        url=result.url,
                        title="",
                        word_count=0,
                        success=False,
                        blocked_by_host=raw_error.startswith("Blocked by anti-bot protection:"),
                        error=_clean_error_message(raw_error),
                    )

                job.pages[result.url] = page
                job.last_progress_at = datetime.now(timezone.utc)
                job.publish(
                    {
                        "type": "page",
                        "page": page.model_dump(),
                        "total_words": job.total_words,
                        "markdown_bytes": job.markdown_bytes,
                        "markdown_pages": job.markdown_pages,
                    }
                )

                if len(job.pages) % _CHECKPOINT_EVERY == 0:
                    await _checkpoint(job, languages)

                # Don't wait for crawl4ai's own should_cancel to notice —
                # BFSDeepCrawlStrategy only checks that between whole BFS
                # levels, which can badly overshoot the word-count pause (or
                # the memory ceiling) if the current level has several slow
                # pages left in it. Cancelling the task directly, right after
                # each result, interrupts sooner than waiting for the rest of
                # the level to drain — though on a genuinely slow/stuck page
                # it can still take up to page_timeout to actually resolve,
                # since that's how long crawl4ai's own cleanup waits for the
                # in-flight fetch to finish or error out. Accepted tradeoff:
                # see page_timeout below for the cheap bound on that case.
                if (
                    job.task is not None
                    and not job.task.done()
                    and (
                        job.cancel_requested
                        or _memory_exceeded()
                        or (pause_at_words is not None and job.total_words >= pause_at_words)
                    )
                ):
                    job.task.cancel()

        job.limit_reached = len(job.pages) + len(job.login_blocked) >= max_pages
        await _resolve_terminal_status(job, pause_at_words, url, scope_filters)
    except asyncio.CancelledError:
        # Reached either via Job.request_cancel() (user clicked Cancel) or
        # the direct self-cancel above (memory/pause) — _resolve_terminal_status
        # inspects the actual job state to tell which one it was, same as the
        # normal-completion path just above.
        job.limit_reached = len(job.pages) + len(job.login_blocked) >= max_pages
        await _resolve_terminal_status(job, pause_at_words, url, scope_filters)
        raise
    except Exception as exc:
        job.status = "failed"
        job.stop_kind = "failed"
        job.error = str(exc)
    finally:
        watchdog_task.cancel()

        # Why a crawl came back smaller than expected is otherwise unanswerable
        # after the fact: the frontier filter's decisions leave no trace in the
        # run, so "it found 439 pages" and "it found 439 pages because 5,000
        # candidates were rejected" look identical. One line per crawl makes the
        # next short report diagnosable from the logs instead of by reproducing
        # it — which is not always possible, since a site that throttled the
        # crawl may well throttle the investigation too.
        if frontier_filter is not None:
            logger.info(
                "crawl %s finished: status=%s pages=%d duplicates=%d concurrency=%d | "
                "frontier rejected %d (pagination=%d duplicate-canonical=%d shape-cap=%d runaway=%d)",
                job.id, job.status, len(job.pages), job.duplicate_count, job.concurrency,
                frontier_filter.rejected_total, frontier_filter.rejected_pagination,
                frontier_filter.rejected_duplicate, frontier_filter.rejected_shape_cap,
                frontier_filter.rejected_runaway,
            )

        # Runs even through the CancelledError re-raise above, so a direct
        # task cancellation still leaves the job in a proper terminal state
        # instead of just vanishing mid-crawl with nothing ever saved.
        job.publish(job.status_payload())

        # Now saved regardless of status, including "paused" — so a paused
        # crawl shows up in "recent runs" too instead of only being visible
        # while its tab happens to still be open. Not picked up by the
        # crash-recovery auto-resume scan (db.get_crawling_runs() only ever
        # looks for status='crawling'), so pausing still never auto-continues
        # on its own — resume_state is included anyway for completeness,
        # matching what's already checkpointed mid-crawl.
        await db.save_run(
            run_id=job.id,
            source_url=job.source_url,
            user_id=job.user_id,
            status=job.status,
            total_words=job.total_words,
            pages=list(job.pages.values()),
            limit_reached=job.limit_reached,
            login_blocked_count=len(job.login_blocked),
            duplicate_count=job.duplicate_count,
            domain_scope=job.domain_scope,
            exclusions=job.exclusions,
            crawl_concurrency=job.concurrency,
            duration_seconds=_elapsed_seconds(job),
            stop_kind=job.stop_kind,
            stopped_reason=job.stopped_reason,
            error=job.error,
            language=",".join(languages) if languages else None,
            language_auto_detected=job.detected_language is not None,
            resume_state=job.resume_state,
            capture_markdown=job.capture_markdown,
            markdown_pages=job.markdown_pages,
            markdown_bytes=job.markdown_bytes,
            markdown_state=job.markdown_state,
            surface=job.surface,
        )

        if job.status == "completed":
            await db.record_estimate_actual(job.id, len(job.pages), job.total_words)

        if job.status in ("completed", "failed", "cancelled"):
            user = await db.get_user(job.user_id)
            if user is not None:
                await send_crawl_notification(
                    to_email=user.email,
                    source_url=job.source_url,
                    status=job.status,
                    total_words=job.total_words,
                    page_count=len(job.pages),
                    run_id=job.id,
                    error=job.error or job.stopped_reason,
                    detected_cms=(job.estimate_result or {}).get("detected_cms"),
                    confidence=(job.estimate_result or {}).get("confidence"),
                    surface=surfaces.for_key(job.surface),
                    # Lets the email make the same "on Pro this would have been
                    # ..." case the report does, from the same numbers.
                    duration_seconds=_elapsed_seconds(job),
                    crawl_concurrency=job.concurrency,
                    is_pro=user.is_pro,
                )

        # This job just freed a slot (or, if paused, was already outside
        # list_active_jobs()'s "starting"/"crawling" set) — see if anything
        # is waiting for it.
        await _maybe_start_next_queued()
