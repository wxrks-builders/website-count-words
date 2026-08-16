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


def _pro_numbers(duration_seconds: int, concurrency: int) -> dict | None:
    """The Pro comparison for a crawl of this length at this speed, or None when
    there is no argument to make."""
    speedup = plans.speedup_over(concurrency)
    if speedup <= 1:
        return None
    pro_seconds = round(duration_seconds / speedup)
    # If the saving rounds away to nothing, saying it would be worse than not.
    if duration_seconds - pro_seconds < 60:
        return None
    return {
        "took_seconds": duration_seconds,
        "pro_seconds": pro_seconds,
        # The same measured figure the pricing page quotes, from the same
        # function, so the two can never drift apart.
        "speedup": plans.advertised_speedup(),
    }


def pro_upsell(
    run: RunRecord | None,
    user: User | None,
    billing_enabled: bool,
    preview: bool = False,
) -> dict | None:
    """What to say about Pro on this report, or None to say nothing.

    Deliberately narrow. It speaks only when every part of the claim is true:
    there is something to sell, this person could buy it, the crawl actually
    finished, it was slow enough to have been annoying, and Pro would genuinely
    have been faster for the speed it ran at.

    preview=True skips all of that for an admin who wants to see the wording —
    including before Stripe is switched on at all, which is otherwise the one
    state where this can never be looked at. It substitutes representative
    numbers for anything the run can't supply, because a run with no recorded
    duration would render "this crawl took less than a minute" and show nothing
    about how the real thing reads.
    """
    if preview:
        numbers = _pro_numbers(
            max(run.duration_seconds if run else 0, PRO_UPSELL_MIN_SECONDS * 3),
            (run.crawl_concurrency if run else 0) or plans.CONCURRENCY_FREE,
        )
        return {**numbers, "preview": True} if numbers else None

    if not billing_enabled or run is None or user is None or user.is_pro:
        return None
    if run.status != "completed":
        return None
    # 0 means unknown — a run from before durations were recorded, or one still
    # going. Claiming a time we don't have would be worse than saying nothing.
    if run.duration_seconds < PRO_UPSELL_MIN_SECONDS:
        return None

    return _pro_numbers(run.duration_seconds, run.crawl_concurrency)


def _domain(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _wxrks_block(source_url: str, total_words: int, medium: str) -> dict:
    """The pitch itself, shared by every placement so they can differ only in
    utm_medium — which is the point, since that's how you tell which one
    converts."""
    domain = _domain(source_url)
    query = urlencode(
        {
            "utm_source": "wordcounter",
            "utm_medium": medium,
            "utm_campaign": "translate",
            "words": total_words,
            "site": domain,
        }
    )
    return {
        "domain": domain,
        "total_words": total_words,
        "url": f"{WXRKS_URL}?{query}",
    }


# A finished report, whether the owner is looking at it or somebody they sent
# the link to. "live" is absent deliberately: a crawl still running has no
# final number to pitch against.
_WXRKS_MODES = ("shared", "past")


def wxrks_pitch(run: RunRecord | None, surface, mode: str, preview: bool = False) -> dict | None:
    """What to say about wxrks on this report, or None.

    Shown to the report's owner and to anyone they share it with. The shared
    link is the more valuable half — it reaches somebody who isn't a user at
    all, and who is looking at a word count because a translation decision is
    being made — but withholding it from owners meant the person who opens this
    app every day never saw it, so new copy shipped unread.

    Counter surface only: someone who came through Site to Markdown is building
    a retrieval pipeline, not shopping for translation.
    """
    if run is None:
        return None
    if preview:
        return {**_wxrks_block(run.source_url, max(run.total_words, WXRKS_MIN_WORDS), "preview"),
                "preview": True}
    if mode not in _WXRKS_MODES:
        return None
    if surface is None or surface.key != surfaces.COUNTER.key:
        return None
    if run.total_words < WXRKS_MIN_WORDS:
        return None
    return _wxrks_block(run.source_url, run.total_words, "shared_report" if mode == "shared" else "report")


def rank_page_promos(pro_block: dict | None, wxrks_block: dict | None, preview: bool = False):
    """At most one promo per page, returned as (pro, wxrks).

    The Ink treatment works by inverting against a page that is otherwise
    entirely light. Two inverted blocks on one report and the contrast stops
    meaning anything — it just reads as two adverts. So this ranks them the same
    way the email does: the reader is the account owner, so if the crawl was
    slow enough to make the case, Pro is the thing they can actually buy;
    otherwise the wxrks pitch, which is also the one that travels when they
    share the report.

    A preview keeps both, because an admin asked to look at them.
    """
    if preview:
        return pro_block, wxrks_block
    if pro_block:
        return pro_block, None
    return None, wxrks_block


def email_promo(
    source_url: str,
    total_words: int,
    status: str,
    surface,
    duration_seconds: int = 0,
    crawl_concurrency: int = 0,
    billing_enabled: bool = False,
    is_pro: bool = False,
) -> dict | None:
    """At most one promo for the finished-crawl email, or None.

    One, never two: the email already has a job to do, and stacking two asks
    onto it is the overreach this whole design exists to avoid. They're ranked
    by what the reader can act on — the recipient is the account owner, so if
    the crawl was slow enough to make the case, Pro is the thing they can
    actually buy. wxrks is the fallback, and it's the one that travels when the
    email gets forwarded.

    Takes primitives rather than a RunRecord because the sender is called from
    the crawl's teardown, which has the numbers but no saved record yet.
    """
    if status != "completed":
        return None

    if billing_enabled and not is_pro and duration_seconds >= PRO_UPSELL_MIN_SECONDS:
        numbers = _pro_numbers(duration_seconds, crawl_concurrency)
        if numbers:
            return {"kind": "pro", **numbers}

    if surface is not None and surface.key == surfaces.COUNTER.key and total_words >= WXRKS_MIN_WORDS:
        return {"kind": "wxrks", **_wxrks_block(source_url, total_words, "crawl_email")}

    return None
