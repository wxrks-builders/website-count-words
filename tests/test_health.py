"""Tests for server health reporting and its alerts.

The point of all this is to answer one question that previously had no answer
anywhere on the server: are crawls dying because too many run at once? That
depends entirely on runs recording *why* they ended, so most of what follows is
about stop_kind being right and being countable.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(Path(__file__).resolve().parents[1] / ".env"))

from app import health  # noqa: E402
from app.models import PageResult  # noqa: E402


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "h.db"))
    monkeypatch.setenv("MARKDOWN_DIR", str(tmp_path / "markdown"))
    import importlib

    import app.db as db
    importlib.reload(db)
    monkeypatch.setattr(health, "db", db)

    run(db.init_db())
    run(db.get_or_create_user("a", "a@x.c", "A", None))
    run(db.get_or_create_user("b", "b@x.c", "B", None))
    yield db
    run(db.close_db())


def save(db, run_id, user_id=1, **kw):
    body = dict(
        run_id=run_id, source_url=f"https://{run_id}.com", user_id=user_id,
        status="completed", total_words=10,
        pages=[PageResult(url=f"https://{run_id}.com/a", word_count=10)],
        limit_reached=False,
    )
    body.update(kw)
    run(db.save_run(**body))


# ------------------------------------------------------------ why runs ended

class TestStopKind:
    """The tag is derived in app/crawler.py; these pin the mapping it produces,
    because a wrong tag is worse than no tag — it would be counted."""

    def test_each_kind_is_stored_and_counted_separately(self, store):
        save(store, "r1", status="cancelled", stop_kind="memory")
        save(store, "r2", status="cancelled", stop_kind="user_cancelled")
        save(store, "r3", status="cancelled", stop_kind="stalled")
        save(store, "r4", status="failed", stop_kind="failed", error="boom")
        save(store, "r5", stop_kind="completed")

        counts = run(store.stop_kind_counts("2000-01-01T00:00:00+00:00"))
        assert counts == {"memory": 1, "user_cancelled": 1, "stalled": 1, "failed": 1, "completed": 1}

    def test_a_run_from_before_this_existed_reads_as_unknown(self, store):
        """Not as "completed" — the whole point is not to invent a reason."""
        save(store, "old", status="cancelled")
        counts = run(store.stop_kind_counts("2000-01-01T00:00:00+00:00"))
        assert counts == {"unknown": 1}

    def test_recent_stops_leaves_out_the_uneventful_ones(self, store):
        save(store, "fine", stop_kind="completed")
        save(store, "paused", status="paused", stop_kind="paused")
        save(store, "killed", status="cancelled", stop_kind="memory",
             stopped_reason="Stopped automatically — too much memory.")

        stops = run(store.recent_stops())
        assert [s["id"] for s in stops] == ["killed"]
        assert "too much memory" in stops[0]["stopped_reason"]

    def test_the_reason_survives_a_reread(self, store):
        save(store, "r", status="failed", stop_kind="failed", error="Playwright exploded")
        record = run(store.get_run("r"))
        assert record.stop_kind == "failed"
        assert record.error == "Playwright exploded"

    def test_a_huge_error_is_truncated_rather_than_stored_whole(self, store):
        save(store, "r", status="failed", stop_kind="failed", error="x" * 5000)
        assert len(run(store.get_run("r")).error) <= 500


# --------------------------------------------------------------- disk by user

class TestDiskByUser:
    def test_bytes_are_summed_per_owner(self, store):
        save(store, "r1", user_id=1, markdown_bytes=1000)
        save(store, "r2", user_id=1, markdown_bytes=2500)
        save(store, "r3", user_id=2, markdown_bytes=400)

        rows = {r["email"]: r for r in run(store.disk_usage_by_user())}
        assert rows["a@x.c"]["bytes"] == 3500
        assert rows["a@x.c"]["runs"] == 2
        assert rows["b@x.c"]["bytes"] == 400

    def test_biggest_first(self, store):
        save(store, "small", user_id=2, markdown_bytes=1)
        save(store, "big", user_id=1, markdown_bytes=999)
        assert [r["email"] for r in run(store.disk_usage_by_user())][0] == "a@x.c"

    def test_a_user_with_no_runs_still_appears(self, store):
        rows = run(store.disk_usage_by_user())
        assert {r["email"] for r in rows} == {"a@x.c", "b@x.c"}
        assert all(r["bytes"] == 0 for r in rows)


# -------------------------------------------------------------------- alerts

class TestAlerts:
    def test_memory_kills_raise_the_concurrency_alarm(self, store, monkeypatch):
        monkeypatch.setattr(health, "MEMORY_KILLS_BEFORE_ALERT", 2)
        save(store, "k1", status="cancelled", stop_kind="memory")
        save(store, "k2", status="cancelled", stop_kind="memory")

        kinds = [a["kind"] for a in run(health.pending_alerts())]
        assert "memory_kills" in kinds

    def test_one_unlucky_crawl_is_not_a_pattern(self, store, monkeypatch):
        monkeypatch.setattr(health, "MEMORY_KILLS_BEFORE_ALERT", 2)
        save(store, "k1", status="cancelled", stop_kind="memory")
        assert "memory_kills" not in [a["kind"] for a in run(health.pending_alerts())]

    def test_old_kills_do_not_keep_firing(self, store, monkeypatch):
        """The window is the last hour, so yesterday's incident stays yesterday's."""
        monkeypatch.setattr(health, "MEMORY_KILLS_BEFORE_ALERT", 1)
        save(store, "k1", status="cancelled", stop_kind="memory")
        future = datetime.now(timezone.utc) + timedelta(days=2)
        assert "memory_kills" not in [a["kind"] for a in run(health.pending_alerts(now=future))]

    def test_the_alert_says_what_tripped_it(self, store, monkeypatch):
        monkeypatch.setattr(health, "MEMORY_KILLS_BEFORE_ALERT", 1)
        save(store, "k1", status="cancelled", stop_kind="memory")
        alert = next(a for a in run(health.pending_alerts()) if a["kind"] == "memory_kills")
        assert "1 crawls" in alert["value"]
        # And what to do about it, since the numbers alone don't say.
        assert "MEMORY_LIMIT_MB" in alert["intro_text"]
        assert "CRAWL_PAGE_BUDGET" in alert["intro_text"]

    def test_the_cooldown_stops_it_mailing_every_cycle(self, store, monkeypatch):
        monkeypatch.setattr(health, "MEMORY_KILLS_BEFORE_ALERT", 1)
        save(store, "k1", status="cancelled", stop_kind="memory")
        now = datetime.now(timezone.utc)

        assert "memory_kills" in [a["kind"] for a in run(health.pending_alerts(now))]
        run(store.mark_alert_sent("memory_kills", now.isoformat()))
        assert "memory_kills" not in [a["kind"] for a in run(health.pending_alerts(now))]

    def test_the_cooldown_expires(self, store, monkeypatch):
        """Tested on _due directly. Going through pending_alerts would also drag
        in the one-hour kill window, and a test that moves the clock forward to
        clear the cooldown moves the kills out of that window at the same time."""
        monkeypatch.setattr(health, "ALERT_COOLDOWN_HOURS", 6)
        now = datetime.now(timezone.utc)
        run(store.mark_alert_sent("memory_kills", now.isoformat()))

        assert run(health._due("memory_kills", now + timedelta(hours=5))) is False
        assert run(health._due("memory_kills", now + timedelta(hours=7))) is True

    def test_cooldown_state_survives_a_restart(self, store):
        """Held in the database rather than in memory, because a process having
        problems is a process that restarts — and would otherwise mail on every
        boot."""
        now = datetime.now(timezone.utc)
        run(store.mark_alert_sent("disk_floor", now.isoformat()))
        assert run(store.alert_last_sent("disk_floor")) == now.isoformat()

    def test_an_unrecognisable_timestamp_does_not_silence_an_alert(self, store):
        run(store.mark_alert_sent("disk_floor", "not-a-date"))
        assert run(health._due("disk_floor", datetime.now(timezone.utc))) is True


# ------------------------------------------------------------------ snapshots

class TestSnapshots:
    def test_live_numbers_are_measured_against_the_real_limits(self):
        from app.crawler import MAX_CONCURRENT_CRAWLS, _MEMORY_LIMIT_BYTES
        from app import plans

        live = health.live_snapshot()
        assert live["memory_limit_bytes"] == _MEMORY_LIMIT_BYTES
        assert live["max_concurrent_crawls"] == MAX_CONCURRENT_CRAWLS
        assert live["page_budget"] == plans.PAGE_BUDGET
        assert live["rss_bytes"] > 0

    def test_disk_is_measured_against_the_caps_the_crawler_enforces(self):
        from app import markdown_store

        disk = health.disk_snapshot()
        assert disk["markdown_cap_bytes"] == markdown_store.MAX_TOTAL_BYTES
        assert disk["floor_bytes"] == markdown_store.DISK_FLOOR_BYTES

    def test_a_directory_that_does_not_exist_yet_is_not_a_full_volume(self, tmp_path, monkeypatch):
        """A fresh install has no Markdown directory. Statting a missing path
        fails, and treating that as zero free space would alarm on day one."""
        from app import markdown_store

        monkeypatch.setattr(markdown_store, "MARKDOWN_DIR", tmp_path / "not-created-yet")
        disk = health.disk_snapshot()
        assert disk["volume_total_bytes"] > 0
        assert disk["below_floor"] is False

    def test_the_full_snapshot_renders_on_an_empty_database(self, store):
        snap = run(health.snapshot())
        assert snap["stop_counts"] == {}
        assert snap["recent_stops"] == []
        assert snap["markdown_bytes_counted"] == 0
