"""Tests for the Stripe webhook — the only thing that grants Pro.

The checkout redirect deliberately grants nothing: a browser can be pointed at
success_url without ever paying. So everything here is about the webhook being
correct, and in particular being idempotent, because Stripe retries deliveries
and sends overlapping events for the same change.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(Path(__file__).resolve().parents[1] / ".env"))

from app import billing  # noqa: E402


def run(coro):
    """A fresh loop per call, matching tests/test_app_flows.py.

    Deliberately not one long-lived loop: aiosqlite's connection is a worker
    thread that binds to whatever loop is running at each call, so reusing one
    loop and then closing it with the connection still open hangs the suite.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def store(monkeypatch, tmp_path):
    """A real database, with the Stripe SDK never actually called."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "b.db"))
    monkeypatch.setenv("MARKDOWN_DIR", str(tmp_path / "markdown"))
    import importlib

    import app.db as db
    importlib.reload(db)
    monkeypatch.setattr(billing, "db", db)

    run(db.init_db())
    user, _ = run(db.get_or_create_user("sub-1", "a@b.c", "A", None))
    yield type("S", (), {"db": db, "run": staticmethod(run), "user": user})()
    # Not optional. init_db() leaves a non-daemon aiosqlite worker thread alive,
    # so without this the interpreter never exits and the suite appears to hang
    # after the last test passes. test_app_flows.py gets this for free from
    # TestClient running the app's lifespan, which calls close_db() on shutdown.
    run(db.close_db())


def subscription(status="active", customer="cus_1", sub_id="sub_1", period_end=1893456000, user_id=None):
    return {
        "id": sub_id,
        "customer": customer,
        "status": status,
        "current_period_end": period_end,
        "metadata": {"user_id": str(user_id)} if user_id else {},
    }


class TestBillingDisabled:
    def test_disabled_when_no_secret_key(self, monkeypatch):
        monkeypatch.setattr(billing, "STRIPE_SECRET_KEY", "")
        assert billing.billing_enabled() is False

    def test_enabled_with_a_key(self, monkeypatch):
        monkeypatch.setattr(billing, "STRIPE_SECRET_KEY", "sk_test_x")
        assert billing.billing_enabled() is True


class TestApplySubscription:
    def test_an_active_subscription_grants_pro(self, store):
        store.run(store.db.set_stripe_customer(store.user.id, "cus_1"))
        store.run(billing._apply_subscription(subscription("active")))

        user = store.run(store.db.get_user(store.user.id))
        assert user.plan == "pro"
        assert user.is_pro is True
        assert user.stripe_subscription_id == "sub_1"
        assert user.plan_renews_at is not None

    def test_trialing_also_grants_pro(self, store):
        store.run(store.db.set_stripe_customer(store.user.id, "cus_1"))
        store.run(billing._apply_subscription(subscription("trialing")))
        assert store.run(store.db.get_user(store.user.id)).is_pro is True

    def test_a_failed_payment_takes_pro_away(self, store):
        store.run(store.db.set_stripe_customer(store.user.id, "cus_1"))
        store.run(billing._apply_subscription(subscription("active")))
        store.run(billing._apply_subscription(subscription("past_due")))

        user = store.run(store.db.get_user(store.user.id))
        assert user.plan == "free"
        assert user.is_pro is False

    def test_redelivery_changes_nothing(self, store):
        """Stripe retries. Handlers write state rather than adjusting it, so the
        second and third deliveries land on exactly the same row."""
        store.run(store.db.set_stripe_customer(store.user.id, "cus_1"))
        store.run(billing._apply_subscription(subscription("active")))
        first = store.run(store.db.get_user(store.user.id))

        for _ in range(3):
            store.run(billing._apply_subscription(subscription("active")))
        assert store.run(store.db.get_user(store.user.id)) == first

    def test_an_unknown_customer_is_ignored_not_fatal(self, store):
        """A webhook for a customer this app has never seen must not raise —
        raising would put Stripe into a retry loop over an event we can't act on.
        """
        store.run(billing._apply_subscription(subscription(customer="cus_unknown")))
        assert store.run(store.db.get_user(store.user.id)).plan == "free"


class TestUserLookup:
    def test_found_by_customer_id(self, store):
        store.run(store.db.set_stripe_customer(store.user.id, "cus_1"))
        found = store.run(billing._user_for(subscription(customer="cus_1")))
        assert found.id == store.user.id

    def test_metadata_is_the_fallback_and_backfills_the_customer(self, store):
        """Covers the account whose customer id was never stored — the event's
        metadata still identifies it, and the lookup is repaired for next time.
        """
        found = store.run(billing._user_for(subscription(customer="cus_9", user_id=store.user.id)))
        assert found.id == store.user.id
        assert store.run(store.db.get_user(store.user.id)).stripe_customer_id == "cus_9"

    def test_no_customer_and_no_metadata_resolves_to_nobody(self, store):
        assert store.run(billing._user_for({"id": "sub_x", "status": "active"})) is None


class TestRenewalDate:
    def test_converted_to_an_iso_timestamp(self):
        assert billing._renews_at({"current_period_end": 1893456000}).startswith("2030-")

    def test_absent_period_end(self):
        assert billing._renews_at({}) is None
