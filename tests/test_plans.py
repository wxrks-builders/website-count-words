"""Tests for crawl-speed tiering.

Crawl duration is almost entirely one number — how many pages are in flight at
once — so these cover who gets what, and the shared budget that keeps several
fast crawls from swamping the box.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(Path(__file__).resolve().parents[1] / ".env"))

from app import plans  # noqa: E402
from app.job_store import JOBS, QUEUE, Job, enqueue, remove_from_queue  # noqa: E402
from app.models import User  # noqa: E402
from app.plans import (  # noqa: E402
    CONCURRENCY_FLOOR,
    active_page_load,
    resolve_concurrency,
    speedup_over,
    tier_concurrency,
)


def user(plan="free", plan_status=None):
    return User(id=1, google_sub="s", email="a@b.c", name="A", plan=plan, plan_status=plan_status)


class TestIsPro:
    def test_active_and_trialing_count(self):
        assert user("pro", "active").is_pro is True
        assert user("pro", "trialing").is_pro is True

    def test_lapsed_subscription_falls_back_to_free(self):
        """plan stays "pro" until Stripe says otherwise, so status is what
        decides — otherwise a failed card keeps its speed indefinitely."""
        assert user("pro", "past_due").is_pro is False
        assert user("pro", "canceled").is_pro is False
        assert user("pro", None).is_pro is False

    def test_plain_free_account(self):
        assert user().is_pro is False


class TestTierConcurrency:
    def test_free_and_pro(self):
        assert tier_concurrency(user()) == plans.CONCURRENCY_FREE
        assert tier_concurrency(user("pro", "active")) == plans.CONCURRENCY_PRO

    def test_anonymous_gets_the_free_tier_not_a_crash(self):
        assert tier_concurrency(None) == plans.CONCURRENCY_FREE

    def test_pro_is_actually_faster(self):
        """The premise of the whole feature."""
        assert plans.CONCURRENCY_PRO > plans.CONCURRENCY_FREE


class TestBudget:
    def test_an_idle_box_gives_the_full_tier(self):
        assert resolve_concurrency(user("pro", "active"), 0) == plans.CONCURRENCY_PRO

    def test_a_busy_box_clamps_to_what_is_left(self):
        spent = plans.PAGE_BUDGET - plans.CONCURRENCY_FREE
        assert resolve_concurrency(user("pro", "active"), spent) == plans.CONCURRENCY_FREE

    def test_a_full_box_still_lets_a_crawl_move(self):
        """A crawl clamped to zero isn't slow, it's hung — the floor is what
        keeps that from happening."""
        assert resolve_concurrency(user("pro", "active"), plans.PAGE_BUDGET) == CONCURRENCY_FLOOR
        assert resolve_concurrency(user(), plans.PAGE_BUDGET * 10) == CONCURRENCY_FLOOR

    def test_a_new_crawl_stays_in_budget_or_only_adds_the_floor(self):
        """The invariant the budget actually provides. It can't be "never
        exceeds the budget", because the floor deliberately wins over it — so
        the guarantee is that going over costs the floor and nothing more."""
        for spent in range(0, plans.PAGE_BUDGET + 10):
            got = resolve_concurrency(user("pro", "active"), spent)
            assert spent + got <= max(plans.PAGE_BUDGET, spent + CONCURRENCY_FLOOR)

    def test_worst_case_load_with_every_slot_taken_by_pro(self):
        """The number that has to fit on the box: admit MAX_CONCURRENT_CRAWLS
        Pro crawls back to back and see what the total page load comes to."""
        from app.crawler import MAX_CONCURRENT_CRAWLS

        spent = 0
        for _ in range(MAX_CONCURRENT_CRAWLS):
            spent += resolve_concurrency(user("pro", "active"), spent)
        # Overshoot is bounded by the floor for each crawl admitted after the
        # budget ran out — never by a multiple of the Pro tier.
        assert spent <= plans.PAGE_BUDGET + MAX_CONCURRENT_CRAWLS * CONCURRENCY_FLOOR
        assert spent < MAX_CONCURRENT_CRAWLS * plans.CONCURRENCY_PRO

    def test_active_page_load_sums_running_crawls(self):
        jobs = [Job(id="a", source_url="u", user_id=1, max_pages=1, concurrency=16),
                Job(id="b", source_url="u", user_id=1, max_pages=1, concurrency=4)]
        assert active_page_load(jobs) == 20

    def test_active_page_load_tolerates_a_job_with_no_concurrency_yet(self):
        jobs = [Job(id="a", source_url="u", user_id=1, max_pages=1)]
        assert active_page_load(jobs) == 0


class TestSpeedup:
    def test_discounted_below_the_raw_page_ratio(self):
        """The measured speedup is sublinear — rendering and extraction compete
        for CPU — so what gets quoted must be less than the ratio of in-flight
        pages, or the product advertises a number it can't hit."""
        raw = plans.CONCURRENCY_PRO / plans.CONCURRENCY_FREE
        assert speedup_over(plans.CONCURRENCY_FREE) < raw
        assert speedup_over(plans.CONCURRENCY_FREE) == raw * plans.SCALING_EFFICIENCY

    def test_the_advertised_multiple_is_within_what_was_measured(self):
        """4 -> 16 measured at 2.79x on a local server with fixed latency. The
        pricing page must not claim more than that."""
        assert plans.advertised_speedup() <= 3

    def test_a_pro_crawl_is_not_offered_a_speedup(self):
        assert speedup_over(plans.CONCURRENCY_PRO) == 1.0

    def test_never_below_one_or_divides_by_zero(self):
        assert speedup_over(0) == 1.0
        assert speedup_over(plans.CONCURRENCY_PRO * 4) == 1.0


class TestQueuePriority:
    def setup_method(self):
        QUEUE.clear()
        JOBS.clear()

    teardown_method = setup_method

    def test_pro_jumps_the_waiting_free_crawls(self):
        enqueue("free-1")
        enqueue("free-2")
        assert enqueue("pro-1", front=True) == 1
        assert QUEUE == ["pro-1", "free-1", "free-2"]

    def test_free_still_joins_the_back(self):
        enqueue("free-1")
        assert enqueue("free-2") == 2
        assert QUEUE == ["free-1", "free-2"]

    def test_jumping_does_not_drop_anyone(self):
        enqueue("free-1")
        enqueue("pro-1", front=True)
        assert remove_from_queue("free-1") is True
        assert QUEUE == ["pro-1"]
