"""Shared machinery for both sides of the pilot.

The point of sharing it: this must be a comparison of FETCHERS, not of URL
policies or extractors. Both runners take the same frontier rules from the
app, and both B and B' go through the one extractor below — so a word delta
can be attributed instead of argued about.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psutil
from lxml import html as lxml_html

# Read-only imports from the app — the pilot never modifies it and nothing in
# the app imports the pilot.
#
# app.url_policy subclasses crawl4ai's URLFilter, but the Scrapling side of the
# pilot deliberately has no crawl4ai in its venv (side A runs in the app's own
# venv — parity by definition, not by pin, since the app's crawl4ai build isn't
# on PyPI). A two-method stub satisfies the subclass; the filter's logic is
# pure Python and identical either way.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    import crawl4ai.deep_crawling.filters  # noqa: F401
except ModuleNotFoundError:
    import types

    class _URLFilterStub:
        def __init__(self, name: str = ""):
            self.name = name

        def _update_stats(self, passed: bool) -> None:
            pass

    _pkg = types.ModuleType("crawl4ai")
    _deep = types.ModuleType("crawl4ai.deep_crawling")
    _filters = types.ModuleType("crawl4ai.deep_crawling.filters")
    _filters.URLFilter = _URLFilterStub
    _pkg.deep_crawling = _deep
    _deep.filters = _filters
    sys.modules["crawl4ai"] = _pkg
    sys.modules["crawl4ai.deep_crawling"] = _deep
    sys.modules["crawl4ai.deep_crawling.filters"] = _filters

from app.url_policy import FrontierDedupeFilter, pagination_param_count  # noqa: E402
from app.word_count import count_words  # noqa: E402

PAGE_CAP = int(__import__("os").environ.get("PILOT_CAP", "120"))
CONCURRENCY = 8

SITES = {
    # name: (seed, note)
    "wxrks": ("https://wxrks.com", "Webflow — production ground truth: 824 pages / 1,076,780 words"),
    "community": ("https://community.clay.com", "JS SPA — crawl4ai discovery collapsed here (5,579 -> 439)"),
    "clay": ("https://www.clay.com", "anti-bot gauntlet — 301 blocked pages in one production run"),
    "wordpress": ("https://wordpress.org/news/", "boring WordPress control"),
    # Added after the first run: clay.com blocked neither side that day, so the
    # anti-bot gate had passed without either fetcher being challenged. Marriott
    # runs Akamai and challenges everyone — it is also the site the app's own
    # comments cite for off-domain redirect cascades.
    "marriott": ("https://www.marriott.com/", "Akamai-protected — the real anti-bot gauntlet"),
}


@dataclass
class PageResult:
    url: str
    ok: bool
    blocked: bool = False
    status: int | None = None
    words: int = 0            # by the runner's own pipeline (A: app markdown path; B: shared extractor)
    words_shared: int = 0     # by the SHARED extractor on this runner's HTML (A's value == B')
    links_found: int = 0
    ms: int = 0
    error: str = ""


@dataclass
class RunResult:
    tool: str
    site: str
    seed: str
    started_at: float = field(default_factory=time.time)
    wall_seconds: float = 0.0
    peak_rss_mb: float = 0.0
    pages: list[PageResult] = field(default_factory=list)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as fh:
            meta = {k: v for k, v in asdict(self).items() if k != "pages"}
            fh.write(json.dumps({"meta": meta}) + "\n")
            for page in self.pages:
                fh.write(json.dumps(asdict(page)) + "\n")


def load(path: Path) -> tuple[dict, list[dict]]:
    lines = path.read_text().splitlines()
    return json.loads(lines[0])["meta"], [json.loads(l) for l in lines[1:]]


# --------------------------------------------------------------- the extractor

_DROP = ("script", "style", "noscript", "template", "svg",
         "nav", "footer", "aside", "form", "header")

_BLOCKED_RE = re.compile(
    r"cf-browser-verification|challenge-platform|Just a moment|Attention Required"
    r"|_Incapsula_|PerimeterX|are you a robot", re.I)


def shared_text(html: str | None) -> str:
    """Visible text by one fixed rule, applied to BOTH tools' HTML.

    Approximates the app's counting posture (its excluded_tags list, plus the
    unarguable script/style class). Deliberately simple: this is the measuring
    stick, and a clever stick is a stick you end up debugging instead of the
    fetchers.
    """
    if not html:
        return ""
    try:
        tree = lxml_html.fromstring(html)
    except Exception:
        return ""
    for tag in _DROP:
        for node in tree.iter(tag):
            node.drop_tree()
    return " ".join(tree.text_content().split())


def shared_words(html: str | None) -> int:
    return count_words(shared_text(html))


def looks_blocked(html: str | None, status: int | None) -> bool:
    """One blocked-detector for both sides. crawl4ai has its own richer one,
    but using it for A only would make the blocked-rate comparison unfair."""
    if status in (403, 429, 503):
        return True
    if html and len(html) < 20_000 and _BLOCKED_RE.search(html):
        return True
    # The soft block: a 200 wearing a challenge interstitial. Marriott's Akamai
    # hands Scrapling a 3KB shell with 5 visible words and one link — which is
    # worse than an honest 403, because a "successful" 5-word homepage would
    # sail into a word-count report as real data. A real page this empty is
    # vanishingly rare on the sites this product crawls.
    if status == 200 and html and len(html) < 10_000 and shared_words(html) < 20:
        return True
    return False


# --------------------------------------------------------------- the frontier

class Frontier:
    """Same admission rules for both runners: the app's dedupe filter, same-host
    scope, the page cap. Both sides ATTEMPT the same universe; only what they
    can fetch differs — that is the entire experiment."""

    def __init__(self, seed: str):
        from urllib.parse import urlsplit

        self.host = urlsplit(seed).netloc.lower().removeprefix("www.")
        self.filter = FrontierDedupeFilter()
        self.seen: set[str] = set()
        self.queue: list[str] = [seed]
        self.seen.add(seed)

    def in_scope(self, url: str) -> bool:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return False
        host = parts.netloc.lower().removeprefix("www.")
        if host != self.host:
            return False
        path = parts.path.lower()
        if any(path.endswith(ext) for ext in (".pdf",".zip",".png",".jpg",".jpeg",".gif",".svg",".webp",".mp4",".css",".js",".ico",".xml")):
            return False
        return True

    def offer(self, url: str) -> None:
        url = url.split("#")[0]
        if url in self.seen or not self.in_scope(url):
            return
        if not self.filter.apply(url):
            return
        self.seen.add(url)
        self.queue.append(url)

    def next_batch(self, n: int) -> list[str]:
        batch, self.queue = self.queue[:n], self.queue[n:]
        return batch


class RssSampler:
    """Peak RSS of this process tree, sampled on a thread — both runners spawn
    browsers, and the browser is most of the memory story."""

    def __init__(self):
        self.peak = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        proc = psutil.Process()
        while not self._stop.is_set():
            try:
                total = proc.memory_info().rss
                for child in proc.children(recursive=True):
                    try:
                        total += child.memory_info().rss
                    except psutil.Error:
                        pass
                self.peak = max(self.peak, total)
            except psutil.Error:
                pass
            self._stop.wait(0.5)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._thread.join(timeout=2)

    @property
    def peak_mb(self) -> float:
        return round(self.peak / 1e6, 1)
