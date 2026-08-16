from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import math
import os
from contextlib import asynccontextmanager
from urllib.parse import quote_plus, urlsplit

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import auth, billing, db, markdown_store, report, surfaces
from app.auth import get_current_user, require_admin, require_user, require_user_api
from app.crawler import MAX_CONCURRENT_CRAWLS, PAUSE_AT_WORDS, estimate_result_from_snapshot, run_crawl
from app.job_store import create_job, enqueue, get_job, list_active_jobs, list_queued_jobs, restore_job
from app.models import CrawlRequest, ResumeRequest, ShareEmailRequest, ShareToggleRequest, User
from app.notifications import absolute_url, remember_origin, send_share_notification
from app.plans import active_page_load, resolve_concurrency
from app.templates import templates
from app.url_policy import parse_exclusions

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = ("completed", "failed", "cancelled", "paused")
# Terminal *and* not resumable — these have nothing left to stream, so the
# crawl page reads them back from the DB instead of using the live view.
_FINISHED_STATUSES = ("completed", "failed", "cancelled")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    # Any run still marked "crawling" here was interrupted by a crash/restart
    # (JOBS is always empty on a fresh process) — pick each one back up from
    # its last checkpoint rather than leaving it stuck forever.
    for run in await db.get_crawling_runs():
        if not await db.claim_crawling_run(run.id):
            # Another process/instance already claimed this one — skip it.
            continue
        job = restore_job(run)
        language = job.language_setting or job.detected_language
        job.task = asyncio.create_task(
            run_crawl(job.id, job.source_url, job.max_pages, job.domain_scope, language, resume_state=job.resume_state)
        )

    # A "paused" run is a legitimate stop, not a crash — but its in-memory
    # Job (and estimate_result) is gone all the same, so without this the
    # crawl page would fall back to the DB-only "past" view, which never
    # shows the estimate panel or lets "Proceed with crawl" work again. No
    # task is started here — just restoring enough state for the page and
    # the existing /resume endpoint to work exactly as they did before the
    # restart.
    for run in await db.get_paused_runs():
        snapshot = await db.get_estimate_snapshot(run.id)
        estimate_result = estimate_result_from_snapshot(snapshot) if snapshot else None
        restore_job(run, estimate_result=estimate_result)

    # Saved Markdown outlives its run if a delete didn't get to unlink it (a
    # crash mid-delete, or a database restored from a backup). Left alone it
    # would sit on the same volume as the database forever.
    try:
        for run_id in markdown_store.existing_run_ids():
            if await db.get_run_exists(run_id):
                continue
            logger.info("Removing saved Markdown for run %s, which no longer exists", run_id)
            markdown_store.delete(run_id)
    except Exception:
        logger.exception("Markdown orphan sweep failed")

    yield
    await db.close_db()


app = FastAPI(lifespan=lifespan)

# Secure by default, opt out for local http:// development.
#
# This used to read RENDER, which the host set automatically — so the moment
# the app moved off Render the flag silently went false and the session cookie
# stopped being marked Secure in production. A missing variable must fail
# closed, not open, so the default is now "on" and only an explicit
# SECURE_COOKIES=false turns it off.
SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "true").strip().lower() not in ("0", "false", "no", "off")
if not SECURE_COOKIES:
    logger.warning("SECURE_COOKIES is off — the session cookie will be sent over plain http. Local development only.")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ["SESSION_SECRET"],
    https_only=SECURE_COOKIES,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(auth.router)
app.include_router(billing.router)


@app.middleware("http")
async def _record_origin(request: Request, call_next):
    """Emails are sent from background tasks that have no request to build a
    link from. Recording the origin real traffic arrives on means a missing
    PUBLIC_BASE_URL degrades to a working link instead of a broken one.

    Also resolves which product this request belongs to — both hostnames are
    served by this one app (see app/surfaces.py). Templates read it through the
    context processor in app/templates.py.
    """
    request.state.surface = surfaces.for_host(request.headers.get("host"))
    remember_origin(str(request.base_url))
    return await call_next(request)


def _valid_url(url: str) -> bool:
    parts = urlsplit(url.strip())
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _sse(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


HOME_RUNS = 10
RUNS_PER_PAGE = 25


def _page_count(total: int, per_page: int) -> int:
    return max(1, math.ceil(total / per_page))


@app.get("/")
async def index(request: Request, user: User | None = Depends(get_current_user)):
    """Signed in, this is the app. Signed out, it's the front door.

    It used to redirect anonymous visitors straight to /login, which meant
    anyone arriving from a link saw a Google button and one sentence about a
    product they'd never heard of.
    """
    if user is None:
        surface = request.state.surface
        return templates.TemplateResponse(
            request,
            "landing.html",
            {
                # Only offered when the run is actually reachable — a stale id
                # should lose the button, not send visitors to a 404.
                "demo_run_id": await _public_demo_run_id(surface),
                "other_surfaces": [s for s in surfaces.SURFACES if s.key != surface.key],
            },
        )

    recent_runs = await db.list_user_runs(user.id, limit=HOME_RUNS)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "user": user,
            "recent_runs": recent_runs,
            "total_runs": await db.count_user_runs(user.id),
        },
    )


async def _public_demo_run_id(surface) -> str:
    run_id = surface.demo_run_id
    if not run_id:
        return ""
    run = await db.get_run(run_id)
    return run_id if run is not None and run.is_public else ""


@app.get("/runs")
async def all_runs(request: Request, page: int = 1, user: User = Depends(require_user)):
    page = max(1, page)
    total = await db.count_user_runs(user.id)
    return templates.TemplateResponse(
        request,
        "runs.html",
        {
            "user": user,
            "runs": await db.list_user_runs(user.id, limit=RUNS_PER_PAGE, offset=(page - 1) * RUNS_PER_PAGE),
            "page": page,
            "page_count": _page_count(total, RUNS_PER_PAGE),
            "total": total,
            "base_url": "/runs?",
        },
    )


@app.post("/crawl")
async def start_crawl(payload: CrawlRequest, request: Request, user: User = Depends(require_user_api)):
    url = payload.url.strip()
    if not _valid_url(url):
        raise HTTPException(status_code=400, detail="Please enter a valid http(s) URL")

    max_pages = float("inf")

    source_url = db.normalize_url(url)

    if not payload.force_recrawl:
        cached = await db.get_latest_run(source_url)
        # Reusing a cached run is only right if it can answer what was asked.
        # Someone who ticked "save Markdown" must not be handed an older run
        # that has none — that would look like the setting silently did nothing.
        # Exclusions are matched for the same reason: handing back a run that
        # still contains the staging mirror you just excluded would read as the
        # field having done nothing.
        reusable = (
            cached is not None
            and (not payload.capture_markdown or cached.markdown_pages > 0)
            and parse_exclusions(cached.exclusions) == parse_exclusions(payload.exclusions)
        )
        if reusable:
            return JSONResponse({"cached": True, "run_id": cached.id})

    # Checked before create_job() below adds itself to JOBS — otherwise the
    # new job would always count toward its own admission check, queueing
    # every request one slot too early (e.g. the 3rd of 3 allowed slots).
    at_capacity = len(list_active_jobs()) >= MAX_CONCURRENT_CRAWLS

    job = create_job(source_url=source_url, user_id=user.id, max_pages=max_pages)
    # Set now (rather than waiting for run_crawl's own copy of this, which
    # only runs once actually started) so a queued job shows correct
    # settings immediately, and _maybe_start_next_queued has the right
    # values to launch with later.
    job.domain_scope = payload.domain_scope
    job.exclusions = payload.exclusions
    # How fast this crawl runs. Resolved here rather than inside run_crawl so a
    # queued job already knows what it was promised, and so the value is fixed
    # against the box as it is right now (see app/plans.py).
    job.concurrency = resolve_concurrency(user, active_page_load(list_active_jobs()))
    job.language_setting = payload.language
    # Set here rather than only passed to run_crawl below, because a queued job
    # is started later by _maybe_start_next_queued, which has no payload — the
    # other resume paths read it off the job for the same reason.
    job.capture_markdown = payload.capture_markdown
    # Recorded from the request, not the payload, so the front door someone
    # actually used is what their finished-crawl email links back to.
    job.surface = request.state.surface.key

    if at_capacity:
        job.status = "queued"
        # Paid crawls jump the waiting free ones. When the box is full this is
        # most of what "faster" means — page concurrency does nothing for a
        # crawl that hasn't started yet.
        position = enqueue(job.id, front=user.is_pro)
        return JSONResponse({"cached": False, "run_id": job.id, "queued": True, "position": position})

    job.task = asyncio.create_task(
        run_crawl(
            job.id, source_url, max_pages, payload.domain_scope, payload.language,
            pause_at_words=PAUSE_AT_WORDS, capture_markdown=payload.capture_markdown,
            exclusions=payload.exclusions,
        )
    )
    return JSONResponse({"cached": False, "run_id": job.id})


@app.post("/crawl/{job_id}/resume")
async def resume_crawl(
    job_id: str,
    # Optional so a browser still running a cached copy of the old app.js —
    # which posts no body at all — keeps working instead of 422ing on Proceed.
    payload: ResumeRequest | None = None,
    user: User = Depends(require_user_api),
):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "paused":
        return JSONResponse({"status": job.status})

    language = job.language_setting or job.detected_language
    job.estimate_result = None
    job.task = asyncio.create_task(
        run_crawl(
            job.id, job.source_url, float("inf"), job.domain_scope, language,
            resume_state=job.resume_state,
            # The estimate panel is the first place anyone can see what the
            # crawl is about to spend itself on, so exclusions can still be
            # tightened here. None means "keep what the crawl already had".
            exclusions=payload.exclusions if payload else None,
        )
    )
    return JSONResponse({"status": "resuming"})


@app.get("/crawl/{run_id}")
async def crawl_page(run_id: str, request: Request, user: User = Depends(require_user)):
    job = get_job(run_id)

    def live_view():
        return templates.TemplateResponse(
            request,
            "crawl.html",
            {
                "mode": "live",
                "run_id": run_id,
                "source_url": job.source_url,
                "started_at": job.started_at,
                "initial_status_payload": job.status_payload(),
                "user": user,
                "share_recipients": share_recipients,
            },
        )

    share_recipients = await db.list_run_shares(run_id)

    # A finished run gets the saved view, even while its job is still in
    # memory. The live view builds its summary from the SSE replay, so on a
    # very large crawl a replay that doesn't make it through leaves the page
    # showing a "Completed" header with no folder or top-pages tables. There
    # is nothing to stream once a run is over, so read it back from the DB
    # and let the page render itself server-side.
    # "paused" is not a finished state here: it's resumable, and the live view
    # is what carries its estimate panel and the Proceed button.
    if job is not None and job.status not in _FINISHED_STATUSES:
        return live_view()

    run = await db.get_run(run_id)
    if run is None:
        # Terminal in memory but not written yet — save_run() runs moments
        # after the status flips. The live view still resolves through SSE.
        if job is not None:
            return live_view()
        raise HTTPException(status_code=404, detail="Crawl not found")

    return templates.TemplateResponse(
        request,
        "crawl.html",
        {
            "mode": "past",
            "run_id": run_id,
            "source_url": run.source_url,
            "run": run,
            "summary": report.summarize(run.pages),
            "initial_pages": [report.page_row(p) for p in run.pages[: report.PAGE_ROWS]],
            "user": user,
            "share_recipients": share_recipients,
        },
    )


@app.get("/share/{run_id}")
async def shared_crawl_page(run_id: str, request: Request):
    run = await db.get_run(run_id)
    if run is None or not run.is_public:
        raise HTTPException(status_code=404, detail="Crawl not found")

    return templates.TemplateResponse(
        request,
        "crawl.html",
        {
            "mode": "shared",
            "run_id": run_id,
            "source_url": run.source_url,
            "run": run,
            "summary": report.summarize(run.pages),
            "initial_pages": [report.page_row(p) for p in run.pages[: report.PAGE_ROWS]],
            "user": None,
            "share_recipients": [],
        },
    )


async def _readable_run(run_id: str, user: User | None):
    """A run the caller may read: their own, or any run with its public link on.
    404 rather than 403 so a private run isn't distinguishable from a missing one."""
    run = await db.get_run(run_id)
    if run is None or not (run.is_public or (user is not None and run.user_id == user.id)):
        raise HTTPException(status_code=404, detail="Crawl not found")
    return run


@app.delete("/crawl/{run_id}")
async def delete_crawl(run_id: str, user: User = Depends(require_user_api)):
    run = await db.get_run(run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="Crawl not found")

    # A crawl still working would carry on writing to a row that no longer
    # exists, and would re-save itself on finishing. Make the caller stop it.
    job = get_job(run_id)
    if job is not None and job.status not in _FINISHED_STATUSES:
        raise HTTPException(status_code=409, detail="Stop this crawl before deleting it")

    await db.delete_run(run_id)
    return JSONResponse({"deleted": True})


@app.get("/crawl/{run_id}/pages")
async def crawl_pages(run_id: str, offset: int = 0, limit: int = report.PAGE_ROWS,
                      user: User | None = Depends(get_current_user)):
    """Rows for the pages table, fetched as the reader asks for more. Embedding
    all of them is what made a large report unopenable."""
    run = await _readable_run(run_id, user)
    offset = max(0, offset)
    limit = max(1, min(limit, 1000))
    rows = run.pages[offset : offset + limit]
    return JSONResponse({
        "pages": [report.page_row(p) for p in rows],
        "offset": offset,
        "total": len(run.pages),
        "has_more": offset + len(rows) < len(run.pages),
    })


@app.get("/crawl/{run_id}/export.csv")
async def export_csv(run_id: str, user: User | None = Depends(get_current_user)):
    """Built and streamed here rather than in the browser — the CSV for a large
    run is bigger than the page should ever hold in memory."""
    run = await _readable_run(run_id, user)
    host = (urlsplit(run.source_url).hostname or "crawl").replace("www.", "")

    def rows():
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        def flush():
            value = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            return value

        writer.writerow(["URL", "Title", "Words", "Status", "Error"])
        # Chunked rather than yielded per row: every yield is its own ASGI
        # message, and 164k of them cost ~20s in overhead alone.
        for i, page in enumerate(run.pages, 1):
            status = ("blocked" if page.blocked_by_host
                      else "login_required" if page.login_required
                      else "ok" if page.success else "failed")
            writer.writerow([
                page.url, page.title or "", page.word_count if page.success else "",
                status, page.error or "",
            ])
            if i % 2000 == 0:
                yield flush()
        yield flush()

    return StreamingResponse(
        rows(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{host}-word-count.csv"'},
    )


@app.get("/crawl/{run_id}/markdown.zip")
async def export_markdown(run_id: str, user: User | None = Depends(get_current_user)):
    """Readable by the owner or through a live public link, matching Export CSV
    and the report itself — a shared report that withholds half its exports
    isn't really shared. Worth knowing this is the app's one large download, so
    a public link is also a standing egress cost until it's switched off."""
    run = await _readable_run(run_id, user)
    if not run.markdown_pages:
        raise HTTPException(status_code=404, detail="No Markdown was saved for this crawl")

    host = (urlsplit(run.source_url).hostname or "crawl").replace("www.", "")
    readme = "\n".join([
        f"Word Counter — saved Markdown for {run.source_url}",
        f"Crawled: {run.created_at}",
        f"Pages in this archive: {run.markdown_pages:,} of {run.page_count:,} crawled",
        "",
        "Main page content only — site navigation, footers and other repeated",
        "chrome are stripped. Each file starts with the URL it came from.",
    ])
    if run.markdown_state.startswith("stopped"):
        readme += (
            "\n\nCapture stopped before the end of the crawl"
            f" ({run.markdown_state.removeprefix('stopped_')}), so this archive covers"
            " only the pages listed above. The word count itself is complete."
        )

    return StreamingResponse(
        markdown_store.iter_zip(run_id, [p.url for p in run.pages], readme=readme),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{host}-markdown.zip"'},
    )


def _share_url(run_id: str, surface=None) -> str | None:
    """Built from the surface the sharer is looking at, so a link copied on the
    Markdown domain doesn't hand the recipient the other brand."""
    return absolute_url(f"/share/{run_id}", surface)


@app.post("/crawl/{run_id}/share")
async def toggle_share(
    run_id: str, request: Request, payload: ShareToggleRequest | None = None,
    user: User = Depends(require_user_api),
):
    run = await db.get_run(run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="Run not found")

    new_state = payload.is_public if payload is not None and payload.is_public is not None else not run.is_public
    await db.set_run_public(run_id, new_state)
    return JSONResponse({"is_public": new_state, "share_url": _share_url(run_id, request.state.surface)})


@app.post("/crawl/{run_id}/share/email")
async def email_share(
    run_id: str, payload: ShareEmailRequest, request: Request, user: User = Depends(require_user_api)
):
    run = await db.get_run(run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="Run not found")

    email = payload.email.strip()
    if not email:
        raise HTTPException(status_code=400, detail="An email address is required")

    # The invite carries the public link, so it can't work while the report is
    # private — inviting someone is itself the intent to make the link work.
    if not run.is_public:
        await db.set_run_public(run_id, True)

    await db.add_run_share(run_id, email)
    await send_share_notification(
        to_email=email,
        # The name reads better than a raw address in "X shared a report with
        # you", but not everyone's Google profile has one worth showing.
        shared_by=user.name or user.email,
        source_url=run.source_url,
        share_url=_share_url(run_id, request.state.surface),
        total_words=run.total_words,
        page_count=run.page_count,
        surface=request.state.surface,
    )
    return JSONResponse(
        {"sent": True, "is_public": True, "recipients": await db.list_run_shares(run_id)}
    )


@app.delete("/crawl/{run_id}/share/recipients")
async def remove_share_recipient(run_id: str, email: str, user: User = Depends(require_user_api)):
    run = await db.get_run(run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="Run not found")

    await db.remove_run_share(run_id, email.strip())
    return JSONResponse({"recipients": await db.list_run_shares(run_id)})


async def _cancel_job(job_id: str) -> str:
    """Cancels a job regardless of which process is actually running it
    (JOBS isn't shared across processes) — sets the in-memory flag if we
    happen to have it locally, but always also persists to the DB so
    whichever process is really running it picks this up on its next poll
    (see crawler.py's _should_cancel). Raises 404 only if the job is
    entirely unknown, both here and in the DB; otherwise returns its
    resulting status (a no-op "as-is" status for an already-terminal job)."""
    job = get_job(job_id)
    if job is not None:
        if job.status in _TERMINAL_STATUSES:
            return job.status
        job.request_cancel()
    else:
        run = await db.get_run(job_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if run.status in _TERMINAL_STATUSES:
            return run.status

    await db.request_cancel(job_id)
    return "cancelling"


@app.post("/crawl/{job_id}/cancel")
async def cancel_crawl(job_id: str, user: User = Depends(require_user_api)):
    status = await _cancel_job(job_id)
    return JSONResponse({"status": status})


@app.get("/events/{job_id}")
async def crawl_events(job_id: str, request: Request, user: User = Depends(require_user_api)):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_stream():
        for page in job.pages.values():
            yield _sse("page", {"type": "page", "page": page.model_dump(), "total_words": job.total_words})
        yield _sse("status", job.status_payload())

        if job.status in _TERMINAL_STATUSES:
            return

        queue: asyncio.Queue = asyncio.Queue()
        job.subscribers.append(queue)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(event["type"], event)
                if event["type"] == "status" and event.get("status") in _TERMINAL_STATUSES:
                    break
        finally:
            if queue in job.subscribers:
                job.subscribers.remove(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _error_pct(estimated: int, actual: int | None) -> float | None:
    if not actual:
        return None
    return round((estimated - actual) / actual * 100, 1)


def _aggregate_estimate_errors(rows: list[dict], group_key: str) -> dict[str, dict]:
    groups: dict[str, list[float]] = {}
    for row in rows:
        if row["word_error_pct"] is None:
            continue
        key = row[group_key] or "(none)"
        groups.setdefault(key, []).append(row["word_error_pct"])
    return {
        key: {
            "count": len(errors),
            "avg_signed_pct": round(sum(errors) / len(errors), 1),
            "avg_abs_pct": round(sum(abs(e) for e in errors) / len(errors), 1),
        }
        for key, errors in groups.items()
    }


def _aggregate_speed_by_concurrency(rows: list[dict]) -> dict[str, dict]:
    groups: dict[str, list[float]] = {}
    for row in rows:
        if row["words_per_minute"] is None or row["concurrent_crawls"] is None:
            continue
        key = str(row["concurrent_crawls"])
        groups.setdefault(key, []).append(row["words_per_minute"])
    return {
        key: {"count": len(speeds), "avg_words_per_minute": round(sum(speeds) / len(speeds))}
        for key, speeds in sorted(groups.items())
    }


@app.get("/admin/estimates")
async def admin_estimates(request: Request, admin: User = Depends(require_admin)):
    rows = await db.list_estimate_history()
    for row in rows:
        row["word_error_pct"] = _error_pct(row["estimated_total_words"], row["actual_total_words"])
        row["page_error_pct"] = _error_pct(row["estimated_total_pages"], row["actual_total_pages"])

    return templates.TemplateResponse(
        request,
        "admin_estimates.html",
        {
            "rows": rows,
            "by_confidence": _aggregate_estimate_errors(rows, "confidence"),
            "by_cms": _aggregate_estimate_errors(rows, "detected_cms"),
            "by_concurrency": _aggregate_speed_by_concurrency(rows),
        },
    )


async def _job_summary(job) -> dict:
    owner = await db.get_user(job.user_id)
    return {
        "id": job.id,
        "source_url": job.source_url,
        "status": job.status,
        "owner_email": owner.email if owner else "(unknown)",
        "started_at": job.started_at,
        "page_count": len(job.pages),
        "total_words": job.total_words,
    }


@app.get("/admin/jobs")
async def admin_jobs(request: Request, admin: User = Depends(require_admin)):
    jobs = [await _job_summary(job) for job in list_active_jobs()]
    queued_jobs = [await _job_summary(job) for job in list_queued_jobs()]
    return templates.TemplateResponse(request, "admin_jobs.html", {"jobs": jobs, "queued_jobs": queued_jobs})


@app.get("/admin/runs")
async def admin_runs(request: Request, page: int = 1, q: str = "", admin: User = Depends(require_admin)):
    page = max(1, page)
    query = q.strip()
    total = await db.count_all_runs(query)
    return templates.TemplateResponse(
        request,
        "admin_runs.html",
        {
            "runs": await db.list_all_runs(limit=RUNS_PER_PAGE, offset=(page - 1) * RUNS_PER_PAGE, query=query),
            "page": page,
            "page_count": _page_count(total, RUNS_PER_PAGE),
            "total": total,
            "query": query,
            "base_url": f"/admin/runs?q={quote_plus(query)}&",
        },
    )


@app.post("/admin/jobs/{job_id}/cancel")
async def admin_cancel_job(job_id: str, admin: User = Depends(require_admin)):
    status = await _cancel_job(job_id)
    return JSONResponse({"status": status})


@app.post("/admin/jobs/cancel-all")
async def admin_cancel_all(admin: User = Depends(require_admin)):
    cancelled = [job.id for job in list_active_jobs() + list_queued_jobs()]
    for job_id in cancelled:
        await _cancel_job(job_id)
    return JSONResponse({"cancelled": cancelled})
