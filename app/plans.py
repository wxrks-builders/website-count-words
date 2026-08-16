"""How fast a crawl is allowed to go, and who decides.

Crawl speed is almost entirely one number. app/crawler.py hands
`semaphore_count` to CrawlerRunConfig, crawl4ai turns it into
`MemoryAdaptiveDispatcher(max_session_permit=N)`, and N is how many pages are
in flight at once. Everything else — depth, filters, markdown — is bookkeeping
around fetches that each take a couple of seconds of network and render time.

It used to be hardcoded at 2, which is where "~6h 37m to crawl clay.com" came
from: 42 pages/min is just 2 pages at a time divided by ~2.85s each. Raising it
is close to a linear speedup until the *site being crawled* becomes the limit,
which is the honest caveat on any number quoted from here.
"""

from __future__ import annotations

import os

from app.models import User

# Free is deliberately no longer 2. The old value was cautious enough to be the
# product's biggest weakness, and 4 is still modest on the box this runs on.
CONCURRENCY_FREE = int(os.environ.get("CRAWL_CONCURRENCY_FREE", "4"))
CONCURRENCY_PRO = int(os.environ.get("CRAWL_CONCURRENCY_PRO", "16"))

# The ceiling on pages in flight across *every* running crawl.
#
# This is the part that isn't optional. crawl4ai builds one dispatcher per
# crawl, so the per-crawl numbers above don't know about each other: without a
# shared budget, MAX_CONCURRENT_CRAWLS Pro crawls would put
# MAX_CONCURRENT_CRAWLS x CONCURRENCY_PRO Chromium pages on the box at once and
# thrash the CPU long before the RSS ceiling in crawler.py noticed.
PAGE_BUDGET = int(os.environ.get("CRAWL_PAGE_BUDGET", "24"))

# No crawl starts slower than this, even with the budget fully spent —
# a crawl that can't fetch anything isn't a slower crawl, it's a stuck one.
CONCURRENCY_FLOOR = 2

# How much of the theoretical speedup actually materializes.
#
# Doubling the in-flight pages does not halve the time, because fetching is only
# part of the work: each page is also rendered, extracted and turned into
# Markdown, and that part competes for CPU. Measured against a local server with
# a fixed 0.3s latency, 120 pages:
#
#     semaphore_count=2  -> 34.8s
#     semaphore_count=4  -> 19.4s   (1.79x, linear model says 2.00x)
#     semaphore_count=16 ->  7.0s   (2.79x over 4, linear model says 4.00x)
#
# 0.7 is the ratio that fits, rounded down. It exists so the number shown to a
# customer is one the product can actually hit — quoting the raw ratio would
# advertise 4x for something that measured 2.8x.
SCALING_EFFICIENCY = float(os.environ.get("CRAWL_SCALING_EFFICIENCY", "0.7"))


def tier_concurrency(user: User | None) -> int:
    """The speed this account is entitled to, before the shared budget."""
    return CONCURRENCY_PRO if (user is not None and user.is_pro) else CONCURRENCY_FREE


def resolve_concurrency(user: User | None, active_concurrency: int = 0) -> int:
    """How many pages this crawl may fetch at once, given what's already running.

    `active_concurrency` is the sum over currently-running crawls (see
    active_page_load). The budget is claimed at start and never rebalanced: a
    crawl that begins while the box is busy stays slow even after the other
    crawls finish. That's a deliberate simplification — reallocating mid-crawl
    would mean rebuilding crawl4ai's dispatcher underneath a running fetch,
    and the queue in job_store.py already keeps the busy case rare.
    """
    remaining = PAGE_BUDGET - max(0, active_concurrency)
    return max(CONCURRENCY_FLOOR, min(tier_concurrency(user), remaining))


def active_page_load(jobs) -> int:
    """Pages in flight across the given jobs — pass list_active_jobs()."""
    return sum(getattr(job, "concurrency", 0) or 0 for job in jobs)


def speedup_over(concurrency: int) -> float:
    """How much faster Pro would actually be than a crawl at `concurrency`.

    Discounted by SCALING_EFFICIENCY rather than quoting the raw ratio of
    in-flight pages, because the raw ratio is not what anyone measures — see
    the numbers next to that constant. Still an estimate, and still assumes
    this app is the bottleneck: a site that rate-limits a heavier crawler caps
    every plan at the same place, which is why the UI hedges the figure.
    """
    if concurrency <= 0 or concurrency >= CONCURRENCY_PRO:
        return 1.0
    return max(1.0, (CONCURRENCY_PRO / concurrency) * SCALING_EFFICIENCY)


def advertised_speedup() -> int:
    """The whole-number multiple shown on the pricing page."""
    return max(1, round(speedup_over(CONCURRENCY_FREE)))
