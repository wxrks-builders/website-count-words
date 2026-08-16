"""When to promote something, and when to stay quiet.

Both promotions here are conditional on being *true* rather than being
permanent furniture, which is the whole design. A banner that always shows is
something people learn to ignore in a week; one that appears because of what
just happened to them is worth reading.

The decisions live here rather than in a template so they can be tested without
a browser, and so "should this appear" is one readable function instead of a
chain of Jinja conditionals nobody can check.
"""

from __future__ import annotations

import os
from urllib.parse import urlencode, urlsplit

from app import plans, surfaces
from app.models import RunRecord, User

WXRKS_URL = os.environ.get("WXRKS_URL", "https://wxrks.com")

# How slow a crawl has to have been before Pro is worth mentioning. Ten minutes
# is the point where somebody went and did something else while it ran — below
# that, faster is not a problem they had.
PRO_UPSELL_MIN_SECONDS = int(os.environ.get("PRO_UPSELL_MIN_SECONDS", "600"))

# A shared report with almost nothing in it is not a translation project, and
# pitching one against it just looks automated.
WXRKS_MIN_WORDS = int(os.environ.get("WXRKS_MIN_WORDS", "5000"))


def pro_upsell(run: RunRecord | None, user: User | None, billing_enabled: bool) -> dict | None:
    """What to say about Pro on this report, or None to say nothing.

    Deliberately narrow. It speaks only when every part of the claim is true:
    there is something to sell, this person could buy it, the crawl actually
    finished, it was slow enough to have been annoying, and Pro would genuinely
    have been faster for the speed it ran at.
    """
    if not billing_enabled or run is None or user is None or user.is_pro:
        return None
    if run.status != "completed":
        return None
    # 0 means unknown — a run from before durations were recorded, or one still
    # going. Claiming a time we don't have would be worse than saying nothing.
    if run.duration_seconds < PRO_UPSELL_MIN_SECONDS:
        return None

    speedup = plans.speedup_over(run.crawl_concurrency)
    if speedup <= 1:
        return None

    pro_seconds = round(run.duration_seconds / speedup)
    # If the saving rounds away to nothing there is no argument to make.
    if run.duration_seconds - pro_seconds < 60:
        return None

    return {
        "took_seconds": run.duration_seconds,
        "pro_seconds": pro_seconds,
        # The same measured figure the pricing page quotes, from the same
        # function, so the two can never drift apart.
        "speedup": plans.advertised_speedup(),
    }


def _domain(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def wxrks_pitch(run: RunRecord | None, surface, mode: str) -> dict | None:
    """What to say about wxrks on this report, or None.

    Shared reports only. That is the one surface reaching people who aren't
    users, and they got here because somebody sent them a word count — which is
    to say, because a translation decision is being made. On any other page
    this would be an advert; here it is the next step of the job in hand.

    Counter surface only, for the same reason inverted: someone who came
    through Site to Markdown is building a retrieval pipeline, not shopping for
    translation.
    """
    if mode != "shared" or run is None:
        return None
    if surface is None or surface.key != surfaces.COUNTER.key:
        return None
    if run.total_words < WXRKS_MIN_WORDS:
        return None

    domain = _domain(run.source_url)
    query = urlencode(
        {
            "utm_source": "wordcounter",
            "utm_medium": "shared_report",
            "utm_campaign": "translate",
            "words": run.total_words,
            "site": domain,
        }
    )
    return {
        "domain": domain,
        "total_words": run.total_words,
        "url": f"{WXRKS_URL}?{query}",
    }
