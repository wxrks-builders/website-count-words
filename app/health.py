"""What the server looks like right now, and when that's worth an email.

Two questions this exists to answer. How much disk everyone is using, and
whether crawls are dying because too many run at once — the second of which was
unanswerable until runs started recording *why* they ended (see `stop_kind` in
app/crawler.py). A memory kill, a stall and someone clicking Cancel all used to
persist as status='cancelled' and nothing else.

Reporting only. Nothing here changes how the app behaves; it measures against
the limits that are already enforced elsewhere — MEMORY_LIMIT_MB in crawler.py,
MAX_TOTAL_BYTES and DISK_FLOOR_BYTES in markdown_store.py, PAGE_BUDGET in
plans.py — so the page can never disagree with what the app actually does.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timedelta, timezone

import psutil

from app import db, markdown_store, notifications, plans
from app.crawler import MAX_CONCURRENT_CRAWLS, _MEMORY_LIMIT_BYTES
from app.job_store import list_active_jobs, list_queued_jobs

logger = logging.getLogger(__name__)

_process = psutil.Process()

# How long an alert stays quiet after firing, so a condition that persists for
# days doesn't mail every cycle.
ALERT_COOLDOWN_HOURS = int(os.environ.get("ALERT_COOLDOWN_HOURS", "6"))
# Markdown total this close to its cap is worth knowing before capture stops.
MARKDOWN_WARN_FRACTION = 0.9
# Memory kills within the last hour before it counts as a pattern rather than
# one unlucky crawl. This is the concurrency signal.
MEMORY_KILLS_BEFORE_ALERT = int(os.environ.get("MEMORY_KILLS_BEFORE_ALERT", "2"))


def _pct(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def live_snapshot() -> dict:
    """The four numbers that make "too many at once" concrete."""
    active = list_active_jobs()
    rss = _process.memory_info().rss
    in_flight = plans.active_page_load(active)
    return {
        "rss_bytes": rss,
        "memory_limit_bytes": _MEMORY_LIMIT_BYTES,
        "memory_pct": _pct(rss, _MEMORY_LIMIT_BYTES),
        "active_crawls": len(active),
        "max_concurrent_crawls": MAX_CONCURRENT_CRAWLS,
        "pages_in_flight": in_flight,
        "page_budget": plans.PAGE_BUDGET,
        "pages_pct": _pct(in_flight, plans.PAGE_BUDGET),
        "queued": len(list_queued_jobs()),
    }


def disk_snapshot(known_run_ids: set[str] | None = None) -> dict:
    """Volume and Markdown store, measured the same way the crawler measures
    them when deciding to stop capturing."""
    try:
        # mkdir first, exactly as markdown_store.disk_is_tight() does. Without
        # it a fresh install has no directory to stat, which falls into the
        # except below and reports zero free space — a false "volume is full"
        # alarm on a machine with nothing on it.
        markdown_store.MARKDOWN_DIR.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(markdown_store.MARKDOWN_DIR)
        total, free = usage.total, usage.free
    except OSError:
        # Same failure posture as markdown_store.disk_is_tight(): if the volume
        # can't be read, assume the worst rather than reporting it as healthy.
        total, free = 0, 0

    on_disk = markdown_store.total_bytes()
    orphans = []
    if known_run_ids is not None:
        orphans = [r for r in markdown_store.existing_run_ids() if r not in known_run_ids]

    return {
        "volume_total_bytes": total,
        "volume_free_bytes": free,
        "volume_used_pct": _pct(total - free, total),
        "floor_bytes": markdown_store.DISK_FLOOR_BYTES,
        "below_floor": free < markdown_store.DISK_FLOOR_BYTES,
        "markdown_bytes_on_disk": on_disk,
        "markdown_cap_bytes": markdown_store.MAX_TOTAL_BYTES,
        "markdown_pct": _pct(on_disk, markdown_store.MAX_TOTAL_BYTES),
        # Directories with no run behind them. The startup sweep clears these,
        # so anything here accumulated since the last boot.
        "orphan_run_dirs": orphans,
    }


async def snapshot() -> dict:
    """Everything the health page shows."""
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    known = await db.known_run_ids()
    disk = disk_snapshot(known)
    by_user = await db.disk_usage_by_user()
    return {
        "live": live_snapshot(),
        # Nothing sends without these, and it fails quietly — which is how
        # "the finished-crawl email has no offer in it" turns out to mean the
        # email was never sent at all.
        "email_configured": notifications.email_configured(),
        "admin_alert_recipients": len(notifications.admin_emails()),
        "disk": disk,
        "by_user": by_user,
        # What the crawler believed it wrote, against what is actually there.
        # Shown side by side because the gap is itself a signal.
        "markdown_bytes_counted": sum(u["bytes"] for u in by_user),
        "stop_counts": await db.stop_kind_counts(week_ago),
        "recent_stops": await db.recent_stops(),
    }


# --------------------------------------------------------------------- alerts

async def _due(kind: str, now: datetime) -> bool:
    last = await db.alert_last_sent(kind)
    if last is None:
        return True
    try:
        return now - datetime.fromisoformat(last) >= timedelta(hours=ALERT_COOLDOWN_HOURS)
    except ValueError:
        return True


async def pending_alerts(now: datetime | None = None) -> list[dict]:
    """Conditions worth an email right now, already filtered by cooldown.

    Each one states the number that tripped it — an alert that says "disk is
    low" without saying how low sends you looking for the thing it already knew.
    """
    now = now or datetime.now(timezone.utc)
    disk = disk_snapshot()
    alerts = []

    if disk["below_floor"]:
        alerts.append({
            "kind": "disk_floor",
            "heading": "Markdown capture has stopped — the volume is nearly full",
            "label": "Free space",
            "value": f"{disk['volume_free_bytes'] / 1e9:.2f} GB "
                     f"(floor is {disk['floor_bytes'] / 1e9:.2f} GB)",
            "intro_text": "Saved Markdown stopped being written because the volume "
                          "shares its disk with the database, which needs the room more. "
                          "Word counts are unaffected.",
        })
    elif disk["markdown_pct"] >= MARKDOWN_WARN_FRACTION * 100:
        alerts.append({
            "kind": "markdown_cap",
            "heading": "Saved Markdown is close to its cap",
            "label": "Markdown stored",
            "value": f"{disk['markdown_bytes_on_disk'] / 1e9:.2f} GB of "
                     f"{disk['markdown_cap_bytes'] / 1e9:.2f} GB ({disk['markdown_pct']}%)",
            "intro_text": "Capture stops on its own at the cap. Nothing is broken yet.",
        })

    hour_ago = (now - timedelta(hours=1)).isoformat()
    kills = await db.count_stops_since("memory", hour_ago)
    if kills >= MEMORY_KILLS_BEFORE_ALERT:
        live = live_snapshot()
        alerts.append({
            "kind": "memory_kills",
            "heading": "Crawls are being stopped for memory",
            "label": "Stopped in the last hour",
            "value": f"{kills} crawls",
            "intro_text": f"The server is at {live['rss_bytes'] / 1e9:.2f} GB of its "
                          f"{live['memory_limit_bytes'] / 1e9:.2f} GB ceiling with "
                          f"{live['active_crawls']} crawls running. Either raise "
                          f"MEMORY_LIMIT_MB if the box has room, or lower "
                          f"CRAWL_PAGE_BUDGET so fewer pages are in flight at once.",
        })

    return [a for a in alerts if await _due(a["kind"], now)]
