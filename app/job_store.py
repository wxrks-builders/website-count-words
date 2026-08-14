from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.models import PageResult

JOBS: dict[str, "Job"] = {}
# FIFO job ids waiting for a free slot (see crawler.py's MAX_CONCURRENT_CRAWLS
# and _maybe_start_next_queued) — not persisted; a restart loses the wait
# list, same acceptable tradeoff as other in-memory-only state in this app.
QUEUE: list[str] = []


@dataclass
class Job:
    id: str
    source_url: str
    user_id: int
    max_pages: int
    status: str = "starting"  # starting | queued | crawling | completed | failed | cancelled | paused
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # Bumped on every page result (success, blocked, or failed) — a stall
    # watchdog (see crawler.py's _stall_watchdog) uses this to detect a crawl
    # that's gone completely silent (a hung fetch/browser context, not just a
    # slow one) and auto-cancel it instead of leaving it "crawling" forever.
    last_progress_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    pages: dict[str, PageResult] = field(default_factory=dict)
    login_blocked: dict[str, PageResult] = field(default_factory=dict)
    total_words: int = 0
    error: str | None = None
    limit_reached: bool = False
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    cancel_requested: bool = False
    detected_language: str | None = None
    domain_scope: str = "all"
    language_setting: str | None = None
    # BFSDeepCrawlStrategy's on_state_change snapshot — lets a paused crawl
    # (see run_crawl's pause_at_words) resume later from the same frontier.
    resume_state: dict | None = None
    estimate_result: dict | None = None
    stopped_reason: str | None = None
    # Only set when reconstructed from a checkpoint (see restore_job) — the
    # crash happened before the specific login-blocked URLs were persisted,
    # just their count, so it's tracked separately from the live dict above.
    restored_login_blocked_count: int = 0
    # Saved page Markdown. Only counters live here — the content itself goes
    # straight to disk (app/markdown_store.py), because holding it would grow
    # RSS against the ceiling that cancels crawls.
    capture_markdown: bool = False
    markdown_pages: int = 0
    markdown_bytes: int = 0
    # off | capturing | stopped_disk | stopped_run_cap | stopped_global_cap | error
    markdown_state: str = "off"
    # The product this run was started from; follows it into the finished email.
    surface: str = "counter"
    # Per-CMS weighted hit tally accumulated across pages (see crawler.py's
    # _detect_cms_signals/_resolve_detected_cms) — not persisted across a
    # crash/resume; low-stakes enough to just start over on that rare path.
    cms_match_counts: dict[str, int] = field(default_factory=dict)
    # The asyncio.Task actually running run_crawl for this job, if it's in
    # this process (never persisted/serialized — purely a runtime handle).
    # crawl4ai's BFSDeepCrawlStrategy only checks its cooperative
    # should_cancel callback between BFS levels, which can take a long time
    # to come back around on a slow site — cancelling this task directly
    # interrupts it immediately, at whatever await point it's currently at.
    task: asyncio.Task | None = None

    def request_cancel(self) -> None:
        # A queued job has no task at all yet — just pull it out of the
        # queue and resolve it directly, rather than the running-crawl path
        # below.
        if self.status == "queued":
            remove_from_queue(self.id)
            self.status = "cancelled"
            return
        # Still set for should_cancel to see (covers the narrow window before
        # a page's first checkpoint, or if .task somehow isn't set) — but
        # .task.cancel() below is what actually makes this prompt.
        self.cancel_requested = True
        if self.task is not None and not self.task.done():
            self.task.cancel()

    def publish(self, event: dict) -> None:
        for queue in list(self.subscribers):
            queue.put_nowait(event)

    def status_payload(self) -> dict:
        return {
            "type": "status",
            "status": self.status,
            "total_words": self.total_words,
            "page_count": len(self.pages),
            "login_blocked_count": len(self.login_blocked) + self.restored_login_blocked_count,
            "limit_reached": self.limit_reached,
            "error": self.error,
            "detected_language": self.detected_language,
            "domain_scope": self.domain_scope,
            "language_setting": self.language_setting,
            "estimate_result": self.estimate_result,
            "stopped_reason": self.stopped_reason,
            "queue_position": (QUEUE.index(self.id) + 1) if self.id in QUEUE else None,
            "markdown": {
                "enabled": self.capture_markdown,
                "pages": self.markdown_pages,
                "bytes": self.markdown_bytes,
                "state": self.markdown_state,
            },
        }


def create_job(source_url: str, user_id: int, max_pages: int) -> Job:
    job = Job(id=uuid.uuid4().hex, source_url=source_url, user_id=user_id, max_pages=max_pages)
    JOBS[job.id] = job
    return job


def get_job(job_id: str) -> Job | None:
    return JOBS.get(job_id)


def list_active_jobs() -> list[Job]:
    """Jobs actively doing work right now — "paused" is deliberately
    excluded: it already exited normally with no live task to interrupt,
    just idle in memory awaiting a Proceed/Adjust decision, a different
    concern from "what's running and needs to be stopped"."""
    return [j for j in JOBS.values() if j.status in ("starting", "crawling")]


def list_queued_jobs() -> list[Job]:
    return [JOBS[job_id] for job_id in QUEUE if job_id in JOBS]


def enqueue(job_id: str) -> int:
    """Adds a job to the back of the wait list, returning its 1-based
    position."""
    QUEUE.append(job_id)
    return len(QUEUE)


def dequeue_next() -> str | None:
    return QUEUE.pop(0) if QUEUE else None


def remove_from_queue(job_id: str) -> bool:
    if job_id in QUEUE:
        QUEUE.remove(job_id)
        return True
    return False


def restore_job(run, estimate_result: dict | None = None) -> Job:
    """Reconstructs an in-memory Job from a checkpointed RunRecord (see
    app.db.get_crawling_runs/get_paused_runs) — status follows the run's own
    (either "crawling", for an interrupted crawl app.main's lifespan will
    relaunch a task for, or "paused", for display/resume purposes only, no
    task started). estimate_result lets a restored "paused" job's estimate
    panel keep working across a restart, since it otherwise only ever lived
    on the original in-memory Job (see app.crawler.estimate_result_from_snapshot)."""
    job = Job(
        id=run.id,
        source_url=run.source_url,
        user_id=run.user_id,
        max_pages=float("inf"),
        status=run.status,
        started_at=run.created_at,
        pages={p.url: p for p in run.pages},
        total_words=run.total_words,
        limit_reached=run.limit_reached,
        detected_language=run.language if run.language_auto_detected else None,
        domain_scope=run.domain_scope,
        language_setting=None if run.language_auto_detected else run.language,
        resume_state=run.resume_state,
        restored_login_blocked_count=run.login_blocked_count,
        estimate_result=estimate_result,
        # Carried across the restart so a resumed crawl keeps capturing instead
        # of silently producing a ZIP that stops at the crash point.
        capture_markdown=run.capture_markdown,
        markdown_pages=run.markdown_pages,
        markdown_bytes=run.markdown_bytes,
        markdown_state=run.markdown_state,
        surface=run.surface,
    )
    JOBS[job.id] = job
    return job
