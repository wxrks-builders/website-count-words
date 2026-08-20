"""Strings that app.js shows, kept here so there is one catalogue.

The alternative — a second JSON catalogue for the front end — means two
extractions, two review passes, and two places a translator has to be sent. A
Python dict costs one render of a small JSON blob per page and keeps every
string this product says in a single .po file.

Keys are stable identifiers; only the English changes when copy is edited, and
pybabel treats a changed msgid as a new string needing translation, which is
correct — reworded copy does need re-translating.
"""

from __future__ import annotations

from app.i18n import gettext_for


def catalogue(lang: str) -> dict[str, str]:
    _ = gettext_for(lang)
    return {
        # Status and progress
        "starting": _("Starting…"),
        "crawling": _("Crawling"),
        "completed": _("Completed"),
        "failed": _("Failed"),
        "cancelled": _("Cancelled"),
        "paused": _("Paused"),
        "queued": _("Queued"),
        "crawl": _("Crawl"),
        "resuming": _("Resuming…"),
        "cancelling": _("Cancelling…"),
        "deleting": _("Deleting…"),

        # The crawl form
        "something_went_wrong": _("Something went wrong"),
        "enter_valid_url": _("Please enter a valid http(s) URL"),
        "add_another": _("Add another…"),
        "exclusions_placeholder": _("staging, web-staging, /careers"),
        "remove_entry": _("Remove %(entry)s"),
        "folder": _("folder"),
        "subdomain": _("subdomain"),

        # Settings pills
        "languages_all": _("Languages: All"),
        "language_one": _("Language: %(lang)s"),
        "language_auto": _("Language: %(lang)s (auto)"),
        "languages_many": _("Languages: %(primary)s + %(rest)s"),
        "excluded_none": _("Excluded: none"),
        "excluded_some": _("Excluded: %(entries)s"),

        # Report tables and totals
        "pages_crawled": _("— %(n)s crawled"),
        "show_all": _("Show all"),
        "root_folder": _("(root)"),
        "no_pages_yet": _("No pages counted yet."),

        # Estimate panel
        "high_confidence": _("High confidence"),
        "medium_confidence": _("Medium confidence"),
        "low_confidence": _("Low confidence"),
        "detected_platform": _("Detected platform: %(cms)s"),
        "detected_contentful": _("Detected platform: Contentful (headless — sitemap conventions vary)"),
        "server_load_easy": _("Easy server load"),
        "server_load_moderate": _("Moderate server load"),
        "server_load_busy": _("Busy server load"),
        "sitemap_found": _("Found a sitemap — this site has approximately %(pages)s pages. Crawling all of them may take a while."),
        "sitemap_missing": _("No sitemap found — this estimate is based only on pages discovered so far (approximately %(pages)s), so it may be less accurate. Crawling all of them may take a while."),
        "words_per_min": _("%(words)s words/min · %(pages)s pages/min"),
        "on_pro": _("about %(time)s on Pro"),
        "see_what_costs": _("See what that costs →"),

        # Durations
        "less_than_a_minute": _("less than a minute"),
        "about_minutes": _("~%(n)s min"),
        "about_hours": _("~%(h)sh"),
        "about_hours_minutes": _("~%(h)sh %(m)sm"),

        # Markdown
        "markdown_download": _("Download Markdown (%(size)s)"),
        "markdown_preparing": _("Preparing…"),

        # Sharing and deleting
        "copy_link": _("Copy link"),
        "copied": _("Copied"),
        "share_sent": _("Sent to %(email)s"),
        "confirm_delete": _("Delete this run and everything saved with it? This cannot be undone."),
        "confirm_cancel_all": _("Cancel every active crawl on the server? This cannot be undone."),

        # Queue
        "queue_position": _("You're number %(n)s in the queue. It'll start on its own."),

        # Billing
        "opening_checkout": _("Opening checkout…"),
        "billing_unreachable": _("Could not reach billing"),
    }
