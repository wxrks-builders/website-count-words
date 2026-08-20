from __future__ import annotations

import logging
import os

import httpx

from app import i18n, surfaces
from app.i18n import N_
from app.templates import _install_language, templates

logger = logging.getLogger(__name__)

MAILGUN_API_KEY = os.environ.get("MAILGUN_API_KEY")
MAILGUN_DOMAIN = os.environ.get("MAILGUN_DOMAIN")
MAILGUN_FROM = os.environ.get("MAILGUN_FROM") or (
    f"Word Counter <noreply@{MAILGUN_DOMAIN}>" if MAILGUN_DOMAIN else None
)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# Where emails should point when PUBLIC_BASE_URL isn't configured. A link in an
# email has no page to be relative to, so "/share/abc" is simply a dead link —
# previously that's what recipients got. The app serves behind --proxy-headers,
# so request.base_url is the real external origin; remembering the most recent
# one lets background jobs (which have no request) still build a working link.
_observed_origin = ""


def remember_origin(origin: str) -> None:
    global _observed_origin
    origin = (origin or "").rstrip("/")
    if origin and origin != _observed_origin:
        _observed_origin = origin
        if not PUBLIC_BASE_URL:
            logger.warning(
                "PUBLIC_BASE_URL is not set — falling back to the observed origin %s for email links. "
                "Set PUBLIC_BASE_URL so links don't depend on which host was hit most recently.",
                origin,
            )


def base_url() -> str:
    return PUBLIC_BASE_URL or _observed_origin


def absolute_url(path: str, surface=None) -> str | None:
    """An absolute link, or None if we genuinely can't build one — callers omit
    the button rather than render a relative href that goes nowhere.

    Pass a surface to link back to the front door the reader actually used. Both
    products are one app on two hostnames, so without this someone who signed up
    on the Markdown domain would get email pointing at the word counter.
    """
    base = surface.origin if surface is not None else base_url()
    if not base:
        logger.error("Cannot build an absolute URL for %s: no PUBLIC_BASE_URL and no request seen yet", path)
        return None
    return f"{base}/{path.lstrip('/')}"


# Marked where they are written, translated at send time — see N_ in app/i18n.py.
_STATUS_SUBJECTS = {
    "completed": N_("Crawl finished"),
    "failed": "Crawl failed",
    "cancelled": N_("Crawl stopped"),
}


def _num(value: int, lang: str) -> str:
    """Grouped for the reader, not the developer — the same rule the pages use."""
    return i18n.format_number(value or 0, lang)


def _fmt(n: int) -> str:
    return f"{n:,}"


def _absolute_promo_logo(promo: dict | None, surface) -> dict | None:
    """A mail client has no origin to resolve a relative path against, so the
    logo has to be absolute by the time the template sees it."""
    if not promo or not promo.get("logo_email_url"):
        return promo
    return {**promo, "logo_email_url": absolute_url(promo["logo_email_url"], surface)}


def email_configured() -> bool:
    return bool(MAILGUN_API_KEY and MAILGUN_DOMAIN)


async def _send_email(to_email: str, subject: str, surface=None, lang: str = i18n.DEFAULT_LANG, **context) -> None:
    if not email_configured():
        # Logged rather than returning in silence: "the email never arrived" is
        # otherwise indistinguishable from "the email was never attempted", and
        # the second one is a five-second fix nobody can see.
        logger.info(
            "Email not sent to %s (%s) — MAILGUN_API_KEY/MAILGUN_DOMAIN not set",
            to_email, subject,
        )
        return

    surface = surface or surfaces.DEFAULT
    # One Jinja environment serves every language, so the catalogue is bound
    # immediately before rendering rather than once at import.
    _install_language(lang)
    html = templates.env.get_template("email_notification.html").render(
        logo_url=absolute_url("/static/brand/logo-email.png", surface),
        product_name=surface.name,
        **context,
    )

    # Plain-text alternative — some clients show it, and spam filters like
    # seeing one that actually matches the HTML.
    lines: list[str] = [context["heading"]]
    if context.get("subheading"):
        lines.append(context["subheading"])
    if context.get("intro_text"):
        lines.append(context["intro_text"])
    for value, label in context.get("hero_stats") or []:
        lines.append(f"{label}: {value}")
    for label, value in context.get("stats") or []:
        lines.append(f"{label}: {value}")
    if context.get("notice"):
        lines.append(context["notice"])
    if context.get("cta_url"):
        lines.append(f"{context.get('cta_label') or 'View'}: {context['cta_url']}")
    text = "\n\n".join(lines)

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages",
                auth=("api", MAILGUN_API_KEY),
                data={"from": MAILGUN_FROM, "to": to_email, "subject": subject, "text": text, "html": html},
                timeout=10,
            )
    except Exception:
        # Best-effort — a notification failure should never affect the crawl
        # itself, which has already finished and saved its results by now.
        logger.exception("Failed to send %r to %s", subject, to_email)


async def send_crawl_notification(
    to_email: str,
    source_url: str,
    status: str,
    total_words: int,
    page_count: int,
    run_id: str,
    error: str | None = None,
    detected_cms: str | None = None,
    confidence: str | None = None,
    surface=None,
    lang: str = i18n.DEFAULT_LANG,
    duration_seconds: int = 0,
    crawl_concurrency: int = 0,
    is_pro: bool = False,
) -> None:
    from app.billing import billing_enabled
    from app.promos import email_promo

    surface = surface or surfaces.DEFAULT
    _ = i18n.gettext_for(lang)
    heading = _(_STATUS_SUBJECTS.get(status, N_("Crawl update")))
    completed = status == "completed"

    # At most one, and only when it's true of this crawl — see app/promos.py.
    # Imported inside the function because app.promos reaches app.surfaces and
    # app.plans, and this module is imported by the crawler at startup.
    promo = email_promo(
        source_url=source_url,
        total_words=total_words,
        status=status,
        surface=surface,
        duration_seconds=duration_seconds,
        crawl_concurrency=crawl_concurrency,
        billing_enabled=billing_enabled(),
        is_pro=is_pro,
        detected_cms=detected_cms,
    )

    hero_stats: list[tuple[str, str]] = []
    stats: list[tuple[str, str]] = []
    notice = None

    if completed:
        intro_text = _("Your crawl finished. Here's what it counted.")
        hero_stats = [(_num(total_words, lang), _("Total words")), (_num(page_count, lang), _("Pages counted"))]
        if detected_cms:
            stats.append((_("Detected platform"), detected_cms))
        if confidence:
            stats.append((_("Estimate confidence"), _(confidence.capitalize())))
    elif status == "cancelled":
        intro_text = _("This crawl was stopped before it finished, so the report shows partial results.")
        hero_stats = [(_num(total_words, lang), _("Words so far")), (_num(page_count, lang), _("Pages so far"))]
        if error:
            notice = error
    else:
        intro_text = _("This crawl couldn't be completed.")
        notice = error or _("No further detail was recorded.")

    await _send_email(
        to_email,
        subject=f"{heading}: {source_url}",
        preheader=(
            _("%(words)s words across %(pages)s pages") % {"words": _num(total_words, lang), "pages": _num(page_count, lang)}
            if completed
            else intro_text
        ),
        heading=heading,
        subheading=source_url,
        intro_text=intro_text,
        hero_stats=hero_stats,
        stats=stats,
        notice=notice,
        notice_tone="danger" if status == "failed" else "warn",
        promo=_absolute_promo_logo(promo, surface),
        pricing_url=absolute_url("/pricing", surface),
        cta_url=absolute_url(f"/crawl/{run_id}", surface),
        cta_label=_("View full report"),
        footer_note=_("You're getting this because you started this crawl on %(product)s.") % {"product": _(surface.name)},
        surface=surface,
        lang=lang,
    )


async def send_share_notification(
    to_email: str,
    shared_by: str,
    source_url: str,
    share_url: str,
    total_words: int | None = None,
    page_count: int | None = None,
    surface=None,
    detected_cms: str | None = None,
    lang: str = i18n.DEFAULT_LANG,
) -> None:
    from app.promos import share_email_promo

    _ = i18n.gettext_for(lang)
    surface = surface or surfaces.DEFAULT
    hero_stats = []
    if total_words is not None and page_count is not None:
        hero_stats = [(_num(total_words, lang), _("Total words")), (_num(page_count, lang), _("Pages counted"))]

    await _send_email(
        to_email,
        subject=_("%(person)s shared a word-count report with you") % {"person": shared_by},
        preheader=_("A word count for %(site)s") % {"site": source_url},
        heading=_("%(person)s shared a report with you") % {"person": shared_by},
        subheading=source_url,
        intro_text=_("They ran a word count on this site and gave you access to the full report."),
        hero_stats=hero_stats,
        stats=[],
        # The recipient isn't a user, which is exactly who this pitch is for.
        promo=_absolute_promo_logo(
            share_email_promo(source_url, total_words, surface, detected_cms), surface),
        pricing_url=absolute_url("/pricing", surface),
        cta_url=share_url,
        cta_label=_("View report"),
        footer_note=_("You're getting this because someone shared a %(product)s report with you.") % {"product": _(surface.name)},
        surface=surface,
        lang=lang,
    )


def admin_emails() -> list[str]:
    """Who gets server alerts. Same list that gates the admin pages, so there is
    one place to add someone rather than two that can disagree."""
    raw = os.environ.get("ADMIN_EMAILS", "")
    return [e.strip() for e in raw.split(",") if e.strip()]


async def send_server_alert(heading: str, intro_text: str, label: str, value: str) -> None:
    """Tells the admins something about the server, not about a crawl.

    Best effort throughout: an alert that raises would take down the periodic
    task that produces every future alert, which is the worst possible failure
    for a thing whose job is to notice failures.
    """
    recipients = admin_emails()
    if not recipients:
        logger.info("Server alert not sent (no ADMIN_EMAILS): %s — %s", heading, value)
        return
    for email in recipients:
        try:
            await _send_email(
                to_email=email,
                subject=f"[{surfaces.DEFAULT.name}] {heading}",
                heading=heading,
                intro_text=intro_text,
                label=label,
                value=value,
                cta_label="Open the health page",
                cta_url=absolute_url("/admin/health"),
                footer_note="You are receiving this because your address is in ADMIN_EMAILS.",
            )
        except Exception:
            logger.exception("Could not send a server alert to %s", email)
