from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiosqlite

from app import markdown_store
from app.models import PageResult, RunRecord, User

DB_PATH = Path(os.environ.get("DB_PATH", "data/wordcount.db"))

_connection: aiosqlite.Connection | None = None


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[: -len(":80")]
    if scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[: -len(":443")]
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


async def init_db() -> None:
    global _connection
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _connection = await aiosqlite.connect(DB_PATH)
    _connection.row_factory = aiosqlite.Row
    await _connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            google_sub TEXT UNIQUE NOT NULL,
            email TEXT NOT NULL,
            name TEXT NOT NULL,
            picture TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            source_url TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            total_words INTEGER NOT NULL,
            page_count INTEGER NOT NULL,
            limit_reached INTEGER NOT NULL,
            pages_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_runs_source_url ON runs(source_url);
        CREATE INDEX IF NOT EXISTS idx_runs_user_id ON runs(user_id);

        CREATE TABLE IF NOT EXISTS server_alerts (
            kind TEXT PRIMARY KEY,
            last_sent_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS run_shares (
            run_id TEXT NOT NULL,
            email TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (run_id, email)
        );

        CREATE TABLE IF NOT EXISTS estimate_history (
            run_id TEXT PRIMARY KEY,
            source_url TEXT NOT NULL,
            created_at TEXT NOT NULL,
            pages_fetched INTEGER NOT NULL,
            discovered_total INTEGER NOT NULL,
            sitemap_count INTEGER,
            sitemap_found INTEGER NOT NULL,
            detected_cms TEXT,
            confidence TEXT NOT NULL,
            estimated_total_pages INTEGER NOT NULL,
            estimated_total_words INTEGER NOT NULL,
            actual_total_pages INTEGER,
            actual_total_words INTEGER,
            completed_at TEXT,
            elapsed_seconds INTEGER,
            words_per_minute INTEGER,
            pages_per_minute REAL,
            estimated_duration_seconds INTEGER,
            concurrent_crawls INTEGER
        );
        """
    )
    await _connection.commit()
    await _ensure_columns()


async def _ensure_columns() -> None:
    """Lightweight migration so existing local databases pick up new columns
    without wiping previously-saved runs."""
    conn = _conn()
    cur = await conn.execute("PRAGMA table_info(runs)")
    existing = {row["name"] for row in await cur.fetchall()}
    if "login_blocked_count" not in existing:
        await conn.execute("ALTER TABLE runs ADD COLUMN login_blocked_count INTEGER NOT NULL DEFAULT 0")
        await conn.commit()
    if "domain_scope" not in existing:
        await conn.execute("ALTER TABLE runs ADD COLUMN domain_scope TEXT NOT NULL DEFAULT 'all'")
        await conn.commit()
    if "language" not in existing:
        await conn.execute("ALTER TABLE runs ADD COLUMN language TEXT")
        await conn.commit()
    if "language_auto_detected" not in existing:
        await conn.execute("ALTER TABLE runs ADD COLUMN language_auto_detected INTEGER NOT NULL DEFAULT 0")
        await conn.commit()
    if "resume_state_json" not in existing:
        await conn.execute("ALTER TABLE runs ADD COLUMN resume_state_json TEXT")
        await conn.commit()
    if "cancel_requested" not in existing:
        await conn.execute("ALTER TABLE runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0")
        await conn.commit()
    if "is_public" not in existing:
        await conn.execute("ALTER TABLE runs ADD COLUMN is_public INTEGER NOT NULL DEFAULT 0")
        await conn.commit()
    if "is_sample" not in existing:
        await conn.execute("ALTER TABLE runs ADD COLUMN is_sample INTEGER NOT NULL DEFAULT 0")
        await conn.commit()
    for column, ddl in [
        # Whether this run was asked to save page Markdown, persisted so a
        # crash-resumed crawl carries on capturing instead of silently stopping.
        ("capture_markdown", "ALTER TABLE runs ADD COLUMN capture_markdown INTEGER NOT NULL DEFAULT 0"),
        ("markdown_pages", "ALTER TABLE runs ADD COLUMN markdown_pages INTEGER NOT NULL DEFAULT 0"),
        ("markdown_bytes", "ALTER TABLE runs ADD COLUMN markdown_bytes INTEGER NOT NULL DEFAULT 0"),
        ("markdown_state", "ALTER TABLE runs ADD COLUMN markdown_state TEXT NOT NULL DEFAULT 'off'"),
        # Which front door this run was started from, so the finished-crawl
        # email links back to the same brand. That email is sent from a
        # background task, which has no request to read a Host header from.
        ("surface", "ALTER TABLE runs ADD COLUMN surface TEXT NOT NULL DEFAULT 'counter'"),
        # Pages fetched and then found to be a copy of one already counted, and
        # the subdomains/folders this run was told to leave out — see
        # app/url_policy.py.
        ("duplicate_count", "ALTER TABLE runs ADD COLUMN duplicate_count INTEGER NOT NULL DEFAULT 0"),
        ("exclusions", "ALTER TABLE runs ADD COLUMN exclusions TEXT"),
        # How many pages this run crawled at once — see app/plans.py. Persisted
        # so a resumed crawl keeps the speed it was started with, rather than
        # silently dropping to whatever the resuming caller resolves.
        ("crawl_concurrency", "ALTER TABLE runs ADD COLUMN crawl_concurrency INTEGER NOT NULL DEFAULT 0"),
        # Wall-clock seconds this run took. created_at alone can't answer "how
        # long did this take", and app/promos.py needs the real number before it
        # will claim anything about it. Runs saved before this default to 0,
        # which reads as "unknown" rather than "instant".
        ("duration_seconds", "ALTER TABLE runs ADD COLUMN duration_seconds INTEGER NOT NULL DEFAULT 0"),
        # Why the run ended. Until these existed, a memory kill, a stall and a
        # user clicking Cancel all persisted as status='cancelled' and nothing
        # else, so "are crawls dying because too many run at once" had no
        # answer anywhere on the server. NULL on older runs means "unknown",
        # which the health page says rather than guessing.
        ("stop_kind", "ALTER TABLE runs ADD COLUMN stop_kind TEXT"),
        ("stopped_reason", "ALTER TABLE runs ADD COLUMN stopped_reason TEXT"),
        ("error", "ALTER TABLE runs ADD COLUMN error TEXT"),
        # Which CMS the crawler recognised while fetching. It was only ever
        # stored on estimate_history, which gets a row only when a crawl pauses
        # for an estimate — so a run that finished without pausing had no
        # platform recorded anywhere. app/promos.py needs it on every run.
        ("detected_cms", "ALTER TABLE runs ADD COLUMN detected_cms TEXT"),
    ]:
        if column not in existing:
            await conn.execute(ddl)
            await conn.commit()

    # The users table has never been migrated before — everything above is the
    # runs table. Billing state lives here because it belongs to the account,
    # not to any one crawl.
    cur = await conn.execute("PRAGMA table_info(users)")
    existing_user_cols = {row["name"] for row in await cur.fetchall()}
    for column, ddl in [
        ("plan", "ALTER TABLE users ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'"),
        ("plan_status", "ALTER TABLE users ADD COLUMN plan_status TEXT"),
        ("stripe_customer_id", "ALTER TABLE users ADD COLUMN stripe_customer_id TEXT"),
        ("stripe_subscription_id", "ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT"),
        ("plan_renews_at", "ALTER TABLE users ADD COLUMN plan_renews_at TEXT"),
    ]:
        if column not in existing_user_cols:
            await conn.execute(ddl)
            await conn.commit()
    if "stripe_customer_id" not in existing_user_cols:
        # A Stripe webhook arrives knowing only the customer id, so that lookup
        # has to be as cheap as the one by primary key.
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_stripe_customer ON users(stripe_customer_id)"
        )
        await conn.commit()

    cur = await conn.execute("PRAGMA table_info(estimate_history)")
    existing_estimate_cols = {row["name"] for row in await cur.fetchall()}
    for column, ddl in [
        ("elapsed_seconds", "ALTER TABLE estimate_history ADD COLUMN elapsed_seconds INTEGER"),
        ("words_per_minute", "ALTER TABLE estimate_history ADD COLUMN words_per_minute INTEGER"),
        ("pages_per_minute", "ALTER TABLE estimate_history ADD COLUMN pages_per_minute REAL"),
        ("estimated_duration_seconds", "ALTER TABLE estimate_history ADD COLUMN estimated_duration_seconds INTEGER"),
        ("concurrent_crawls", "ALTER TABLE estimate_history ADD COLUMN concurrent_crawls INTEGER"),
    ]:
        if column not in existing_estimate_cols:
            await conn.execute(ddl)
            await conn.commit()


async def close_db() -> None:
    global _connection
    if _connection is not None:
        await _connection.close()
        _connection = None


def _conn() -> aiosqlite.Connection:
    if _connection is None:
        raise RuntimeError("Database not initialized — call init_db() at startup")
    return _connection


def _row_to_user(row: aiosqlite.Row) -> User:
    return User(
        id=row["id"],
        google_sub=row["google_sub"],
        email=row["email"],
        name=row["name"],
        picture=row["picture"],
        plan=row["plan"],
        plan_status=row["plan_status"],
        stripe_customer_id=row["stripe_customer_id"],
        stripe_subscription_id=row["stripe_subscription_id"],
        plan_renews_at=row["plan_renews_at"],
    )


async def get_or_create_user(google_sub: str, email: str, name: str, picture: str | None) -> tuple[User, bool]:
    """Returns the user and whether this call is what created them — the caller
    seeds the sample run on a genuinely new account, and only once."""
    conn = _conn()
    async with conn.execute("SELECT * FROM users WHERE google_sub = ?", (google_sub,)) as cur:
        row = await cur.fetchone()
    if row is not None:
        return _row_to_user(row), False

    now = datetime.now(timezone.utc).isoformat()
    cur = await conn.execute(
        "INSERT INTO users (google_sub, email, name, picture, created_at) VALUES (?, ?, ?, ?, ?)",
        (google_sub, email, name, picture, now),
    )
    await conn.commit()
    return await get_user(cur.lastrowid), True


async def get_user(user_id: int) -> User | None:
    conn = _conn()
    async with conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_user(row) if row else None


async def get_user_by_stripe_customer(customer_id: str) -> User | None:
    """How a Stripe webhook finds the account: the event carries a customer id
    and nothing else that identifies us."""
    conn = _conn()
    async with conn.execute("SELECT * FROM users WHERE stripe_customer_id = ?", (customer_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_user(row) if row else None


async def set_stripe_customer(user_id: int, customer_id: str) -> None:
    conn = _conn()
    await conn.execute("UPDATE users SET stripe_customer_id = ? WHERE id = ?", (customer_id, user_id))
    await conn.commit()


async def save_subscription_state(
    user_id: int,
    plan: str,
    plan_status: str | None,
    stripe_subscription_id: str | None,
    plan_renews_at: str | None,
) -> None:
    """Writes plan state outright rather than adjusting it, which is what makes
    a redelivered Stripe webhook a no-op instead of a double-apply."""
    conn = _conn()
    await conn.execute(
        """
        UPDATE users
           SET plan = ?, plan_status = ?, stripe_subscription_id = ?, plan_renews_at = ?
         WHERE id = ?
        """,
        (plan, plan_status, stripe_subscription_id, plan_renews_at, user_id),
    )
    await conn.commit()


def _row_to_run(row: aiosqlite.Row) -> RunRecord:
    pages = [PageResult(**p) for p in json.loads(row["pages_json"])]
    resume_state_json = row["resume_state_json"]
    return RunRecord(
        id=row["id"],
        source_url=row["source_url"],
        user_id=row["user_id"],
        created_at=row["created_at"],
        status=row["status"],
        total_words=row["total_words"],
        page_count=row["page_count"],
        limit_reached=bool(row["limit_reached"]),
        login_blocked_count=row["login_blocked_count"],
        duplicate_count=row["duplicate_count"],
        domain_scope=row["domain_scope"],
        exclusions=row["exclusions"],
        crawl_concurrency=row["crawl_concurrency"],
        duration_seconds=row["duration_seconds"],
        stop_kind=row["stop_kind"],
        stopped_reason=row["stopped_reason"],
        error=row["error"],
        detected_cms=row["detected_cms"],
        language=row["language"],
        language_auto_detected=bool(row["language_auto_detected"]),
        resume_state=json.loads(resume_state_json) if resume_state_json else None,
        is_public=bool(row["is_public"]),
        is_sample=bool(row["is_sample"]),
        capture_markdown=bool(row["capture_markdown"]),
        markdown_pages=row["markdown_pages"],
        markdown_bytes=row["markdown_bytes"],
        markdown_state=row["markdown_state"],
        surface=row["surface"],
        pages=pages,
    )


async def get_latest_run(source_url: str) -> RunRecord | None:
    conn = _conn()
    async with conn.execute(
        "SELECT * FROM runs WHERE source_url = ? AND status = 'completed' ORDER BY created_at DESC LIMIT 1",
        (source_url,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_run(row) if row else None


async def get_run(run_id: str) -> RunRecord | None:
    conn = _conn()
    async with conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_run(row) if row else None


async def set_run_public(run_id: str, is_public: bool) -> bool:
    """Returns False if the run has no saved row at all (e.g. still live and
    never reached a terminal/paused state) — nothing to toggle yet."""
    conn = _conn()
    cur = await conn.execute("UPDATE runs SET is_public = ? WHERE id = ?", (int(is_public), run_id))
    await conn.commit()
    return cur.rowcount == 1


async def add_run_share(run_id: str, email: str) -> None:
    """Records that the report's link was emailed to someone. This is a record
    of who it was sent to, not an access grant — access comes from is_public."""
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    # DO NOTHING, not REPLACE: re-sending to someone already on the list keeps
    # their original invite date, so the list doesn't reshuffle under them.
    await conn.execute(
        "INSERT INTO run_shares (run_id, email, created_at) VALUES (?, ?, ?)"
        " ON CONFLICT(run_id, email) DO NOTHING",
        (run_id, email, now),
    )
    await conn.commit()


async def list_run_shares(run_id: str) -> list[dict]:
    conn = _conn()
    async with conn.execute(
        "SELECT email, created_at FROM run_shares WHERE run_id = ? ORDER BY created_at", (run_id,)
    ) as cur:
        rows = await cur.fetchall()
    return [{"email": row["email"], "created_at": row["created_at"]} for row in rows]


async def remove_run_share(run_id: str, email: str) -> None:
    conn = _conn()
    await conn.execute("DELETE FROM run_shares WHERE run_id = ? AND email = ?", (run_id, email))
    await conn.commit()


async def save_run(
    run_id: str,
    source_url: str,
    user_id: int,
    status: str,
    total_words: int,
    pages: list[PageResult],
    limit_reached: bool,
    login_blocked_count: int = 0,
    duplicate_count: int = 0,
    domain_scope: str = "all",
    exclusions: str | None = None,
    crawl_concurrency: int = 0,
    duration_seconds: int = 0,
    stop_kind: str | None = None,
    stopped_reason: str | None = None,
    error: str | None = None,
    detected_cms: str | None = None,
    language: str | None = None,
    language_auto_detected: bool = False,
    resume_state: dict | None = None,
    capture_markdown: bool = False,
    markdown_pages: int = 0,
    markdown_bytes: int = 0,
    markdown_state: str = "off",
    surface: str = "counter",
) -> None:
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    pages_json = json.dumps([p.model_dump() for p in pages])
    resume_state_json = json.dumps(resume_state) if resume_state is not None else None
    await conn.execute(
        """
        INSERT INTO runs
            (id, source_url, user_id, created_at, status, total_words, page_count, limit_reached,
             login_blocked_count, duplicate_count, domain_scope, exclusions, crawl_concurrency,
             duration_seconds, stop_kind, stopped_reason, error, detected_cms,
             language, language_auto_detected, resume_state_json, pages_json,
             capture_markdown, markdown_pages, markdown_bytes, markdown_state, surface)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            status=excluded.status,
            total_words=excluded.total_words,
            page_count=excluded.page_count,
            limit_reached=excluded.limit_reached,
            login_blocked_count=excluded.login_blocked_count,
            duplicate_count=excluded.duplicate_count,
            domain_scope=excluded.domain_scope,
            exclusions=excluded.exclusions,
            crawl_concurrency=excluded.crawl_concurrency,
            duration_seconds=excluded.duration_seconds,
            stop_kind=excluded.stop_kind,
            stopped_reason=excluded.stopped_reason,
            error=excluded.error,
            detected_cms=excluded.detected_cms,
            language=excluded.language,
            language_auto_detected=excluded.language_auto_detected,
            resume_state_json=excluded.resume_state_json,
            pages_json=excluded.pages_json,
            capture_markdown=excluded.capture_markdown,
            markdown_pages=excluded.markdown_pages,
            markdown_bytes=excluded.markdown_bytes,
            markdown_state=excluded.markdown_state,
            surface=excluded.surface
        """,
        (
            # created_at is only ever set on first insert (see ON CONFLICT above) —
            # periodic checkpointing during a crawl must not keep bumping it forward.
            run_id, source_url, user_id, now, status, total_words, len(pages), int(limit_reached),
            login_blocked_count, duplicate_count, domain_scope, exclusions, crawl_concurrency,
            duration_seconds, stop_kind, stopped_reason, (error or "")[:500] or None, detected_cms,
            language, int(language_auto_detected), resume_state_json, pages_json,
            int(capture_markdown), markdown_pages, markdown_bytes, markdown_state, surface,
        ),
    )
    await conn.commit()


async def get_crawling_runs() -> list[RunRecord]:
    """Runs still marked "crawling" at startup are, by definition, orphaned —
    JOBS is always empty on a fresh process, so nothing else could still be
    running one. Used to auto-resume crawls interrupted by a crash/restart.

    First resets any rows stuck at "resuming" (claimed by claim_crawling_run,
    then crashed again before reaching a checkpoint) back to "crawling" so
    they're eligible again — safe to run redundantly if multiple processes
    boot at once, since the end state is identical either way."""
    conn = _conn()
    await conn.execute("UPDATE runs SET status = 'crawling' WHERE status = 'resuming'")
    await conn.commit()
    async with conn.execute("SELECT * FROM runs WHERE status = 'crawling'") as cur:
        rows = await cur.fetchall()
    return [_row_to_run(row) for row in rows]


async def get_paused_runs() -> list[RunRecord]:
    """Runs still marked "paused" at startup — JOBS is always empty on a
    fresh process, so their in-memory Job (and its estimate_result) is gone
    even though the pause itself is a legitimate, not-crashed state. Used to
    rebuild each one back into JOBS (display-only, no task relaunched — see
    app.main's lifespan) so the estimate panel and "Proceed with crawl" keep
    working across a restart instead of quietly breaking."""
    conn = _conn()
    async with conn.execute("SELECT * FROM runs WHERE status = 'paused'") as cur:
        rows = await cur.fetchall()
    return [_row_to_run(row) for row in rows]


async def get_estimate_snapshot(run_id: str) -> dict | None:
    conn = _conn()
    async with conn.execute("SELECT * FROM estimate_history WHERE run_id = ?", (run_id,)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def claim_crawling_run(run_id: str) -> bool:
    """Atomically claims an orphaned run for auto-resume, so if more than one
    process/instance races to resume the same run on startup, only one wins.
    Must only ever match the exact "crawling" state (not e.g. "resuming" too)
    — SQLite serializes this UPDATE across processes sharing the same DB
    file, so whichever one flips the row first leaves nothing for the other
    to match."""
    conn = _conn()
    cur = await conn.execute(
        "UPDATE runs SET status = 'resuming' WHERE id = ? AND status = 'crawling'",
        (run_id,),
    )
    await conn.commit()
    return cur.rowcount == 1


async def request_cancel(run_id: str) -> None:
    """Persists a cancel request to the shared DB (not just in-memory), so it
    reaches whichever process is actually running the crawl even if the HTTP
    request that triggered it landed on a different one. A no-op if the run
    has no row yet (cancelled before its first checkpoint) — the in-memory
    flag set alongside this call covers that narrow window instead."""
    conn = _conn()
    await conn.execute("UPDATE runs SET cancel_requested = 1 WHERE id = ?", (run_id,))
    await conn.commit()


async def is_cancel_requested(run_id: str) -> bool:
    conn = _conn()
    async with conn.execute("SELECT cancel_requested FROM runs WHERE id = ?", (run_id,)) as cur:
        row = await cur.fetchone()
    return bool(row["cancel_requested"]) if row else False


async def save_estimate_snapshot(run_id: str, source_url: str, estimate_result: dict) -> None:
    """Called exactly once per run, right when it pauses and computes an
    estimate — a run only ever pauses once (resuming never sets
    pause_at_words again), so there's nothing to upsert against here."""
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        """
        INSERT INTO estimate_history
            (run_id, source_url, created_at, pages_fetched, discovered_total, sitemap_count,
             sitemap_found, detected_cms, confidence, estimated_total_pages, estimated_total_words,
             elapsed_seconds, words_per_minute, pages_per_minute, estimated_duration_seconds, concurrent_crawls)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, source_url, now,
            estimate_result["pages_fetched"], estimate_result["discovered_total"], estimate_result["sitemap_count"],
            int(estimate_result["sitemap_found"]), estimate_result["detected_cms"], estimate_result["confidence"],
            estimate_result["total_pages_estimate"], estimate_result["estimated_total_words"],
            estimate_result["elapsed_seconds"], estimate_result["words_per_minute"],
            estimate_result["pages_per_minute"], estimate_result["estimated_duration_seconds"],
            estimate_result["concurrent_crawls"],
        ),
    )
    await conn.commit()


async def record_estimate_actual(run_id: str, actual_total_pages: int, actual_total_words: int) -> None:
    """Safe to call unconditionally whenever a run completes — a no-op
    (affects zero rows) for any run that never paused and so never had an
    estimate snapshot saved in the first place."""
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        "UPDATE estimate_history SET actual_total_pages = ?, actual_total_words = ?, completed_at = ? WHERE run_id = ?",
        (actual_total_pages, actual_total_words, now, run_id),
    )
    await conn.commit()


async def list_estimate_history() -> list[dict]:
    conn = _conn()
    async with conn.execute("SELECT * FROM estimate_history ORDER BY created_at DESC") as cur:
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


# Listing screens show one line per run and never touch the page rows, so they
# deliberately avoid pages_json — parsing it for every run is what would make
# "all runs" expensive on an account (or a server) with a lot of history.
_RUN_SUMMARY_COLUMNS = ("id, source_url, created_at, status, total_words, page_count, is_public, is_sample,"
                        " markdown_pages, markdown_bytes, detected_cms")


def _row_to_run_summary(row: aiosqlite.Row) -> dict:
    summary = {
        "id": row["id"],
        "source_url": row["source_url"],
        "created_at": row["created_at"],
        "status": row["status"],
        "total_words": row["total_words"],
        "page_count": row["page_count"],
        "is_public": bool(row["is_public"]),
        "is_sample": bool(row["is_sample"]),
        "markdown_pages": row["markdown_pages"],
        "markdown_bytes": row["markdown_bytes"],
        "detected_cms": row["detected_cms"],
    }
    if "owner_email" in row.keys():
        summary["owner_email"] = row["owner_email"]
        summary["owner_name"] = row["owner_name"]
    return summary


async def delete_run(run_id: str) -> None:
    """Removes the run and everything hanging off it. estimate_history is kept
    deliberately — it's the admin accuracy record, and dropping rows would
    quietly bias it toward whichever runs nobody happened to delete."""
    conn = _conn()
    await conn.execute("DELETE FROM run_shares WHERE run_id = ?", (run_id,))
    await conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    await conn.commit()
    markdown_store.delete(run_id)


async def copy_run_to_user(run: RunRecord, user_id: int, run_id: str, as_sample: bool = False) -> None:
    """Note the markdown_* columns are deliberately absent from the INSERT below,
    so the copy takes their defaults. Stored Markdown is keyed by run id on disk
    and is not copied — inheriting the counts would give the new run a download
    button pointing at another run's files."""
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        "INSERT INTO runs (id, source_url, user_id, created_at, status, total_words, page_count,"
        " limit_reached, pages_json, login_blocked_count, domain_scope, language,"
        " language_auto_detected, is_sample)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id, run.source_url, user_id, now, run.status, run.total_words, run.page_count,
            int(run.limit_reached), json.dumps([p.model_dump() for p in run.pages]),
            run.login_blocked_count, run.domain_scope, run.language,
            int(run.language_auto_detected), int(as_sample),
        ),
    )
    await conn.commit()


async def get_run_exists(run_id: str) -> bool:
    """Existence only — deliberately not get_run(), which parses the whole
    pages_json blob just to answer this."""
    conn = _conn()
    async with conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)) as cur:
        return await cur.fetchone() is not None


async def count_user_runs(user_id: int) -> int:
    conn = _conn()
    async with conn.execute("SELECT COUNT(*) AS n FROM runs WHERE user_id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
    return row["n"]


async def list_user_runs(user_id: int, limit: int = 10, offset: int = 0) -> list[dict]:
    conn = _conn()
    async with conn.execute(
        f"SELECT {_RUN_SUMMARY_COLUMNS} FROM runs WHERE user_id = ?"
        " ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (user_id, limit, offset),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_run_summary(row) for row in rows]


def _all_runs_filter(query: str) -> tuple[str, list]:
    """Admin search matches the crawled site or the owner's email/name."""
    if not query:
        return "", []
    like = f"%{query}%"
    return " WHERE r.source_url LIKE ? OR u.email LIKE ? OR u.name LIKE ?", [like, like, like]


async def count_all_runs(query: str = "") -> int:
    conn = _conn()
    where, params = _all_runs_filter(query)
    async with conn.execute(
        f"SELECT COUNT(*) AS n FROM runs r JOIN users u ON u.id = r.user_id{where}", params
    ) as cur:
        row = await cur.fetchone()
    return row["n"]


async def disk_usage_by_user(limit: int = 25) -> list[dict]:
    """Who is holding how much saved Markdown, largest first.

    markdown_bytes is what the crawler counted as it wrote. The health page
    shows it next to what is actually on disk rather than picking one, because
    the two can drift — an orphaned run directory survives until the next boot
    sweep, and a crash mid-run leaves bytes nobody counted."""
    conn = _conn()
    async with conn.execute(
        """
        SELECT u.id, u.email, u.name,
               COALESCE(SUM(r.markdown_bytes), 0) AS bytes,
               COUNT(r.id) AS runs,
               SUM(CASE WHEN r.markdown_pages > 0 THEN 1 ELSE 0 END) AS runs_with_markdown
          FROM users u LEFT JOIN runs r ON r.user_id = u.id
      GROUP BY u.id
      ORDER BY bytes DESC, runs DESC
         LIMIT ?
        """,
        (limit,),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def stop_kind_counts(since_iso: str) -> dict[str, int]:
    """How runs ended since a given time, by kind. The panel that answers
    "are crawls dying because too many run at once"."""
    conn = _conn()
    async with conn.execute(
        "SELECT COALESCE(stop_kind, 'unknown') AS kind, COUNT(*) AS n"
        " FROM runs WHERE created_at >= ? GROUP BY kind ORDER BY n DESC",
        (since_iso,),
    ) as cur:
        return {row["kind"]: row["n"] for row in await cur.fetchall()}


async def recent_stops(limit: int = 15) -> list[dict]:
    """The most recent runs that didn't simply finish, with their reasons."""
    conn = _conn()
    async with conn.execute(
        """
        SELECT r.id, r.source_url, r.created_at, r.status, r.stop_kind,
               r.stopped_reason, r.error, r.duration_seconds, r.crawl_concurrency,
               u.email AS owner_email
          FROM runs r JOIN users u ON u.id = r.user_id
         WHERE r.stop_kind IS NOT NULL AND r.stop_kind NOT IN ('completed', 'paused')
      ORDER BY r.created_at DESC LIMIT ?
        """,
        (limit,),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def count_stops_since(kind: str, since_iso: str) -> int:
    conn = _conn()
    async with conn.execute(
        "SELECT COUNT(*) AS n FROM runs WHERE stop_kind = ? AND created_at >= ?",
        (kind, since_iso),
    ) as cur:
        return (await cur.fetchone())["n"]


async def known_run_ids() -> set[str]:
    """Every run id the database knows about, for spotting orphaned Markdown
    directories between boots — the startup sweep only runs at boot."""
    conn = _conn()
    async with conn.execute("SELECT id FROM runs") as cur:
        return {row["id"] for row in await cur.fetchall()}


async def alert_last_sent(kind: str) -> str | None:
    conn = _conn()
    async with conn.execute("SELECT last_sent_at FROM server_alerts WHERE kind = ?", (kind,)) as cur:
        row = await cur.fetchone()
    return row["last_sent_at"] if row else None


async def mark_alert_sent(kind: str, when_iso: str) -> None:
    """Persisted rather than held in memory on purpose: an in-process cooldown
    resets on restart, and restarts are exactly what happens when the server is
    having the problems these alerts are about."""
    conn = _conn()
    await conn.execute(
        "INSERT INTO server_alerts (kind, last_sent_at) VALUES (?, ?)"
        " ON CONFLICT(kind) DO UPDATE SET last_sent_at = excluded.last_sent_at",
        (kind, when_iso),
    )
    await conn.commit()


async def list_all_runs(limit: int = 50, offset: int = 0, query: str = "") -> list[dict]:
    """Every user's runs, newest first — admin only."""
    conn = _conn()
    where, params = _all_runs_filter(query)
    columns = ", ".join(f"r.{c}" for c in _RUN_SUMMARY_COLUMNS.split(", "))
    async with conn.execute(
        f"SELECT {columns}, u.email AS owner_email, u.name AS owner_name"
        f" FROM runs r JOIN users u ON u.id = r.user_id{where}"
        " ORDER BY r.created_at DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_run_summary(row) for row in rows]
