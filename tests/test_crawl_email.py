"""Tests for the finished-crawl email, and the one promo it may carry.

Rendered through the real Jinja template rather than asserted on a dict. The
suite never rendered this email before, which is exactly how an email template
breaks without anyone noticing: nothing fails, the mail just doesn't arrive.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv(str(Path(__file__).resolve().parents[1] / ".env"))

from app import notifications, promos, surfaces  # noqa: E402

SLOW = 3 * 3600


@pytest.fixture()
def sent(monkeypatch):
    """Captures the rendered HTML instead of posting it to Mailgun."""
    captured = {}

    async def fake_post(url, **kwargs):
        captured["data"] = kwargs.get("data", {})

        class R:
            status_code = 200
            text = "ok"

        return R()

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        post = staticmethod(fake_post)

    class FakeHttpx:
        """Replaces the name `httpx` inside notifications only. Patching
        httpx.AsyncClient itself breaks authlib, which subclasses it at import
        time — and authlib is imported lazily further down this call chain."""

        AsyncClient = staticmethod(lambda *a, **k: FakeClient())

    monkeypatch.setattr(notifications, "MAILGUN_API_KEY", "key")
    monkeypatch.setattr(notifications, "MAILGUN_DOMAIN", "mg.example.com")
    monkeypatch.setattr(notifications, "MAILGUN_FROM", "Word Counter <n@mg.example.com>")
    monkeypatch.setattr(notifications, "httpx", FakeHttpx)
    return captured


def send(**kw):
    body = dict(
        to_email="a@b.c", source_url="https://www.clay.com", status="completed",
        total_words=662_000, page_count=100, run_id="r", surface=surfaces.COUNTER,
    )
    body.update(kw)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(notifications.send_crawl_notification(**body))
    finally:
        loop.close()


class TestTheEmailStillWorks:
    def test_a_completed_crawl_renders(self, sent):
        """The template compiling at all — the thing nothing checked before."""
        send()
        html = sent["data"]["html"]
        assert "662,000" in html
        assert "View full report" in html

    def test_a_failed_crawl_renders_its_reason(self, sent):
        send(status="failed", error="Playwright exploded")
        assert "Playwright exploded" in sent["data"]["html"]


class TestOnePromoAtMost:
    def test_a_slow_crawl_is_offered_pro_when_there_is_pro_to_sell(self, sent, monkeypatch):
        monkeypatch.setattr(notifications, "MAILGUN_API_KEY", "key")
        from app import billing

        monkeypatch.setattr(billing, "STRIPE_SECRET_KEY", "sk_test_x")
        send(duration_seconds=SLOW, crawl_concurrency=4)

        html = sent["data"]["html"]
        assert "Compare plans" in html
        assert "words to translate" not in html, "two asks in one email is the overreach"

    def test_otherwise_it_falls_back_to_wxrks(self, sent):
        """Billing is off here, so there is no Pro to sell — but the word count
        still travels when this email gets forwarded."""
        send(duration_seconds=SLOW, crawl_concurrency=4)

        html = sent["data"]["html"]
        assert "words to translate" in html
        assert "Compare plans" not in html

    def test_a_crawl_that_did_not_finish_is_sold_nothing(self, sent):
        for status in ("failed", "cancelled"):
            send(status=status, duration_seconds=SLOW, crawl_concurrency=4)
            html = sent["data"]["html"]
            assert "words to translate" not in html, status
            assert "Compare plans" not in html, status

    def test_a_small_site_is_not_called_a_translation_project(self, sent):
        send(total_words=200)
        assert "words to translate" not in sent["data"]["html"]

    def test_the_markdown_product_is_not_sold_translation(self, sent):
        send(surface=surfaces.MARKDOWN)
        assert "words to translate" not in sent["data"]["html"]

    def test_the_link_says_it_came_from_the_email(self, sent):
        """So the report and email placements can be told apart in analytics."""
        send()
        assert "utm_medium=crawl_email" in sent["data"]["html"]

        block = promos.email_promo(
            source_url="https://www.clay.com", total_words=662_000,
            status="completed", surface=surfaces.COUNTER,
        )
        assert parse_qs(urlsplit(block["url"]).query)["utm_medium"] == ["crawl_email"]

    def test_a_pro_subscriber_is_not_sold_pro(self, sent, monkeypatch):
        from app import billing

        monkeypatch.setattr(billing, "STRIPE_SECRET_KEY", "sk_test_x")
        send(duration_seconds=SLOW, crawl_concurrency=4, is_pro=True)
        assert "Compare plans" not in sent["data"]["html"]
