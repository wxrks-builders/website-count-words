"""Product identities served from one app, selected by the request's Host header.

The word counter and the Markdown extractor are marketed as two products, but
they are one crawl: Markdown is a flag on it, and a page's Markdown is only ever
saved after that page's words have been counted (see _capture_markdown in
crawler.py). Splitting them into two deployments would duplicate the crawler for
a boolean, and two instances can't share this app's state — job progress lives
in process memory, and the startup orphan sweep would delete the other
instance's saved Markdown.

So both hostnames hit the same service and the same account. A surface only
changes what the visitor is told and what the form defaults to; everything
behind it is shared, which is the point — someone who arrives for Markdown and
signs in still sees their history, and can be shown the other half.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from app.i18n import N_


@dataclass(frozen=True)
class Surface:
    key: str
    host: str
    name: str
    tagline: str
    # Landing page
    headline: str
    lede: str
    points: list[tuple[str, str]]
    demo_label: str
    # Social + search
    description: str
    og_image: str
    default_capture_markdown: bool = False
    demo_run_id: str = field(default="")

    @property
    def origin(self) -> str:
        return f"https://{self.host}"

    def url(self, path: str = "/") -> str:
        return f"{self.origin}/{path.lstrip('/')}"


COUNTER = Surface(
    key="counter",
    host=os.environ.get("COUNTER_HOST", "wordcounter.wxrks.app"),
    name=N_("Word Counter"),
    tagline=N_("Count every word on a website"),
    headline=N_("Nobody knows how many words are on their website."),
    lede=N_(
        "Not the marketing lead, not the person who built it, and not the agency about to "
        "quote you for translating it. Point this at a URL and find out — page by page, "
        "folder by folder."
    ),
    points=[
        (
            N_("Built for translation quotes"),
            N_(
                "Translation is priced per word, so the first question is one most site owners "
            "can only guess at. Guessing wrong by a factor of three is a budget that runs "
            "out halfway through."
            ),
        ),
        (
            N_("It counts what a translator is handed"),
            N_(
                "Navigation, footers, sidebars and forms are stripped before counting, so a "
            "500-page site doesn't inflate by the same menu repeated 500 times."
            ),
        ),
        (
            N_("It sees pages the way a reader does"),
            N_(
                "Every page is rendered in a real browser, so content built by JavaScript is "
            "counted. Tools that only fetch HTML report a fraction of the truth."
            ),
        ),
        (
            N_("Broken down where it's useful"),
            N_(
                "Totals per folder and per page, your longest pages ranked, and a CSV of every "
            "URL — so a quote turns into a plan you can phase."
            ),
        ),
    ],
    demo_label=N_("See a real report"),
    description=N_(
        "Crawl any website and count every word — per page, per folder, in total. Built for "
        "sizing translation projects, with CSV export and a shareable report."
    ),
    og_image="/static/brand/og-counter.png",
    default_capture_markdown=False,
    demo_run_id=os.environ.get("COUNTER_DEMO_RUN_ID", ""),
)


MARKDOWN = Surface(
    key="markdown",
    host=os.environ.get("MARKDOWN_HOST", "markdown.wxrks.app"),
    name=N_("Site to Markdown"),
    tagline=N_("Turn any website into clean Markdown"),
    headline=N_("Your website, as clean text a language model can actually read."),
    lede=N_(
        "Point this at a URL and get every page back as Markdown, in one ZIP — main content "
        "only, no navigation, no cookie banners, no markup burning tokens without carrying "
        "meaning."
    ),
    points=[
        (
            N_("Main content, not the whole page"),
            N_(
                "Each page runs through a content filter that drops navigation and repeated "
            "chrome. That's the difference between a corpus you can search and one where "
            "every document matches the phrase “Book Now”."
            ),
        ),
        (
            N_("One file per page, provenance intact"),
            N_(
                "Every file opens with the URL it came from, so nothing loses its source on the "
            "way into a retrieval pipeline."
            ),
        ),
        (
            N_("The token arithmetic is the argument"),
            N_(
                "English runs near 1.3 tokens per word. A 662,000-word site is about 880,000 "
            "tokens of clean text — as raw HTML it would be several times that, nearly all "
            "of it markup."
            ),
        ),
        (
            N_("Ready for whatever comes next"),
            N_(
                "Ground a support assistant in your own answers, audit content at a scale nobody "
            "can read, brief translators with real terminology, or move platforms without "
            "losing the writing."
            ),
        ),
    ],
    demo_label=N_("See what comes out"),
    description=N_(
        "Crawl any website and export every page as clean Markdown in a single ZIP. Main "
        "content only — ready to feed a language model, a RAG pipeline or a CMS migration."
    ),
    og_image="/static/brand/og-markdown.png",
    default_capture_markdown=True,
    demo_run_id=os.environ.get("MARKDOWN_DEMO_RUN_ID", ""),
)


SURFACES = (COUNTER, MARKDOWN)
DEFAULT = COUNTER

_BY_HOST = {s.host.lower(): s for s in SURFACES}


def for_host(host: str | None) -> Surface:
    """The surface a request belongs to.

    Falls back to the default rather than erroring: local development, Render's
    own *.onrender.com hostname and health checks all arrive on hosts nobody
    configured, and none of them should 500.
    """
    if not host:
        return DEFAULT
    # Host carries the port in development (localhost:8000).
    return _BY_HOST.get(host.split(":")[0].strip().lower(), DEFAULT)


def for_key(key: str | None) -> Surface:
    """Used where only a stored key is available — a crawl-finished email is sent
    from a background task with no request to read a host from."""
    for surface in SURFACES:
        if surface.key == key:
            return surface
    return DEFAULT
