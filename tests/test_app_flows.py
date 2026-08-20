"""End-to-end coverage of the routes, over a throwaway database.

These started life as ad-hoc scripts in a scratch directory and were lost twice
when it was cleaned, so they live here now. They exercise the paths that are
easy to break from a distance: saving a run, the report view, sharing, the
Markdown export and its access rules, deleting, and the two hostnames.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(Path(__file__).resolve().parents[1] / ".env"))


@pytest.fixture()
def app_env(monkeypatch, tmp_path):
    """A fresh app with its own database and Markdown store."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("MARKDOWN_DIR", str(tmp_path / "markdown"))
    monkeypatch.setenv("SAMPLE_RUN_URL", "https://sample.example")
    monkeypatch.setenv("COUNTER_DEMO_RUN_ID", "demo")
    monkeypatch.setenv("MARKDOWN_DEMO_RUN_ID", "demo")

    import importlib

    import app.markdown_store as markdown_store
    import app.surfaces as surfaces
    importlib.reload(markdown_store)
    importlib.reload(surfaces)

    import app.db as db
    import app.main as main
    importlib.reload(db)
    importlib.reload(main)

    from fastapi.testclient import TestClient

    from app.auth import get_current_user, require_user, require_user_api

    from app.models import User

    # Real User models rather than ad-hoc stubs: the routes and templates read
    # whatever the User model exposes (plan state, is_pro), and a stub that
    # drifts from it fails as a template error rather than as a useful test.
    owner = User(id=1, google_sub="a", email="o@x.c", name="Owner")
    other = User(id=2, google_sub="b", email="e@x.c", name="Other")
    pro = User(id=1, google_sub="a", email="o@x.c", name="Owner", plan="pro", plan_status="active")
    current = {"user": owner}

    main.app.dependency_overrides[require_user] = lambda: current["user"]
    main.app.dependency_overrides[require_user_api] = lambda: current["user"]
    main.app.dependency_overrides[get_current_user] = lambda: current["user"]

    with TestClient(main.app) as client:
        run(db.get_or_create_user("a", "o@x.c", "Owner", None))
        run(db.get_or_create_user("b", "e@x.c", "Other", None))
        yield type("Env", (), {
            "client": client, "db": db, "main": main, "surfaces": surfaces,
            "store": markdown_store, "owner": owner, "other": other, "pro": pro,
            "current": current,
        })()
    main.app.dependency_overrides.clear()


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def pages(n=8):
    from app.models import PageResult

    return [
        PageResult(url=f"https://x.com/f{i % 3}/p{i}", title=f"Title {i}", word_count=100 + i)
        for i in range(n)
    ]


def save(env, run_id="r", **kw):
    body = dict(
        run_id=run_id, source_url="https://x.com", user_id=1, status="completed",
        total_words=840, pages=pages(), limit_reached=False,
    )
    body.update(kw)
    run(env.db.save_run(**body))
    return run_id


# ----------------------------------------------------------------- the report

def test_a_saved_run_renders_its_report(app_env):
    save(app_env)
    body = app_env.client.get("/crawl/r").text
    assert "initialPages" in body, "the report should be served from the database"
    assert '"total_pages": 8' in body or "summary" in body


def test_listing_pages_render(app_env):
    for i in range(12):
        save(app_env, run_id=f"r{i}")
    assert app_env.client.get("/").status_code == 200
    assert app_env.client.get("/runs").status_code == 200
    assert "See all" in app_env.client.get("/").text


def test_csv_export_covers_every_page(app_env):
    save(app_env)
    body = app_env.client.get("/crawl/r/export.csv").text
    assert body.count("\n") == 9, "header plus one row per page"
    assert "URL,Title,Words,Status,Error" in body


# ------------------------------------------------------------------- sharing

def test_share_link_grants_and_revokes_access(app_env):
    save(app_env)
    app_env.current["user"] = app_env.other
    assert app_env.client.get("/share/r").status_code == 404

    app_env.current["user"] = app_env.owner
    app_env.client.post("/crawl/r/share", json={"is_public": True})
    del app_env.main.app.dependency_overrides[
        __import__("app.auth", fromlist=["get_current_user"]).get_current_user
    ]
    assert app_env.client.get("/share/r").status_code == 200
    assert app_env.client.get("/crawl/r/export.csv").status_code == 200


def test_a_private_run_is_not_readable_by_others(app_env):
    save(app_env)
    app_env.current["user"] = app_env.other
    assert app_env.client.get("/crawl/r/pages").status_code == 404
    assert app_env.client.get("/crawl/r/export.csv").status_code == 404
    assert app_env.client.get("/crawl/r/markdown.zip").status_code == 404
    assert app_env.client.request("DELETE", "/crawl/r").status_code == 404
    assert app_env.client.post("/crawl/r/share", json={"is_public": True}).status_code == 404


@pytest.mark.xfail(
    reason="Known gap: GET /crawl/{id} has no ownership check, so any signed-in user "
           "can open another user's report if they have the id. Same for /resume, "
           "/cancel and /events. Ids are unguessable UUIDs, so it isn't trivially "
           "exploitable, but it is not enforced. Reported and left as-is pending a "
           "decision, since restricting it changes behaviour people may rely on. "
           "This test starts passing the day it's fixed.",
    strict=True,
)
def test_the_report_page_should_also_be_owner_only(app_env):
    save(app_env)
    app_env.current["user"] = app_env.other
    assert app_env.client.get("/crawl/r").status_code == 404


# ------------------------------------------------------------------ Markdown

def test_markdown_zip_round_trips(app_env):
    for page in pages():
        app_env.store.write("r", page.url, f"# {page.title}\n\nbody of {page.title}")
    save(app_env, capture_markdown=True, markdown_pages=8, markdown_bytes=999,
         markdown_state="capturing")

    res = app_env.client.get("/crawl/r/markdown.zip")
    assert res.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(res.content))
    assert archive.testzip() is None
    assert len(archive.namelist()) == 9, "one per page plus the README"
    assert "body of Title 0" in archive.read(
        app_env.store.entry_name("https://x.com/f0/p0")).decode()


def test_markdown_is_not_offered_when_none_was_saved(app_env):
    save(app_env)
    assert app_env.client.get("/crawl/r/markdown.zip").status_code == 404


def test_deleting_a_run_removes_its_markdown(app_env):
    app_env.store.write("r", "https://x.com/f0/p0", "body")
    save(app_env, capture_markdown=True, markdown_pages=1, markdown_bytes=99)
    assert app_env.store.run_bytes("r") > 0

    assert app_env.client.request("DELETE", "/crawl/r").status_code == 200
    assert app_env.store.run_bytes("r") == 0
    assert run(app_env.db.get_run("r")) is None


def test_the_sample_run_copy_carries_no_markdown(app_env):
    """Every new account gets a copy of the sample run. Inheriting the Markdown
    counters would give it a download button pointing at another run's files."""
    save(app_env, run_id="tmpl", capture_markdown=True, markdown_pages=5, markdown_bytes=500,
         markdown_state="capturing")
    template = run(app_env.db.get_run("tmpl"))
    run(app_env.db.copy_run_to_user(template, 2, "copy", as_sample=True))

    copy = run(app_env.db.get_run("copy"))
    assert copy.markdown_pages == 0
    assert copy.markdown_bytes == 0
    assert copy.markdown_state == "off"
    assert copy.is_sample


# ------------------------------------------------------------------ surfaces

def counter_host(env):
    return {"host": env.surfaces.COUNTER.host}


def markdown_host(env):
    return {"host": env.surfaces.MARKDOWN.host}


def test_the_front_door_is_public(app_env):
    app_env.current["user"] = None
    res = app_env.client.get("/", headers=counter_host(app_env))
    assert res.status_code == 200, "anonymous visitors get a landing page, not a redirect"
    assert app_env.surfaces.COUNTER.headline in res.text
    assert app_env.client.get("/login").status_code == 200, "the health check depends on this"


def test_each_hostname_gets_its_own_identity(app_env):
    app_env.current["user"] = None
    counter = app_env.client.get("/", headers=counter_host(app_env)).text
    markdown = app_env.client.get("/", headers=markdown_host(app_env)).text

    assert app_env.surfaces.MARKDOWN.headline in markdown
    assert app_env.surfaces.COUNTER.headline in counter
    assert "og-markdown.png" in markdown and "og-counter.png" not in markdown
    assert "og-counter.png" in counter and "og-markdown.png" not in counter
    for body, surface in ((counter, app_env.surfaces.COUNTER), (markdown, app_env.surfaces.MARKDOWN)):
        assert f'rel="canonical" href="https://{surface.host}/"' in body
        assert surface.description in body


@pytest.mark.parametrize("host", ["x-abc.onrender.com", "localhost:8000", "", "unknown.example"])
def test_unknown_hosts_fall_back_rather_than_error(app_env, host):
    app_env.current["user"] = None
    res = app_env.client.get("/", headers={"host": host} if host else {})
    assert res.status_code == 200
    assert app_env.surfaces.COUNTER.headline in res.text


def test_the_markdown_door_pre_ticks_the_checkbox(app_env):
    assert 'id="capture-markdown" checked' in app_env.client.get(
        "/", headers=markdown_host(app_env)).text
    assert 'id="capture-markdown">' in app_env.client.get(
        "/", headers=counter_host(app_env)).text


def test_signed_in_users_still_get_the_app(app_env):
    body = app_env.client.get("/", headers=counter_host(app_env)).text
    assert 'id="crawl-form"' in body
    assert app_env.surfaces.COUNTER.headline not in body, "landing copy must not leak into the app"


def test_reports_are_kept_out_of_search_results(app_env):
    """A shared report is somebody's data on a public URL."""
    save(app_env)
    assert 'content="noindex' in app_env.client.get("/crawl/r").text
    run(app_env.db.set_run_public("r", True))
    assert 'content="noindex' in app_env.client.get("/share/r").text


def test_the_demo_link_only_appears_when_it_works(app_env):
    app_env.current["user"] = None
    save(app_env, run_id="demo")
    run(app_env.db.set_run_public("demo", True))
    assert "/share/demo" in app_env.client.get("/", headers=counter_host(app_env)).text

    run(app_env.db.set_run_public("demo", False))
    assert "/share/demo" not in app_env.client.get("/", headers=counter_host(app_env)).text


def test_the_surface_is_recorded_on_the_run(app_env):
    """The finished-crawl email is sent from a background task with no request,
    so the front door has to be persisted with the run."""
    save(app_env, surface="markdown")
    assert run(app_env.db.get_run("r")).surface == "markdown"
    assert run(app_env.db.get_run("r")).surface != app_env.surfaces.COUNTER.key


# ------------------------------------------------------------------- billing

def test_billing_routes_do_not_exist_when_stripe_is_unconfigured(app_env):
    """With no key the whole feature is off, and 404 rather than 503 because
    the UI never links to it either — same shape as email with no Mailgun key."""
    assert app_env.client.get("/pricing").status_code == 404
    assert app_env.client.post("/billing/checkout").status_code == 404
    assert app_env.client.post("/stripe/webhook", content=b"{}").status_code == 404


def test_the_upgrade_prompt_only_shows_when_there_is_something_to_buy(app_env, monkeypatch):
    from app import billing

    assert "/pricing" not in app_env.client.get("/").text

    monkeypatch.setattr(billing, "STRIPE_SECRET_KEY", "sk_test_x")
    assert "/pricing" in app_env.client.get("/").text


def test_a_pro_account_is_offered_billing_not_an_upgrade(app_env, monkeypatch):
    from app import billing

    monkeypatch.setattr(billing, "STRIPE_SECRET_KEY", "sk_test_x")
    app_env.current["user"] = app_env.pro
    body = app_env.client.get("/").text
    assert "plan-pill" in body, "a paying account should be marked as such"
    assert "/pricing" not in body, "and not asked to upgrade again"


def test_the_pricing_page_never_promises_more_than_was_measured(app_env, monkeypatch):
    """Guards the number itself. Pro fetches 4x the pages but measured 2.8x, and
    the page must quote the smaller one — see plans.SCALING_EFFICIENCY."""
    from app import billing, plans

    monkeypatch.setattr(billing, "STRIPE_SECRET_KEY", "sk_test_x")
    body = app_env.client.get("/pricing").text
    raw = round(plans.CONCURRENCY_PRO / plans.CONCURRENCY_FREE)
    assert f"{plans.advertised_speedup()}× faster" in body
    assert plans.advertised_speedup() < raw


def test_a_webhook_without_a_valid_signature_is_rejected(app_env, monkeypatch):
    """The plan is only ever written from a signature-verified event — a browser
    can reach success_url without having paid."""
    from app import billing

    monkeypatch.setattr(billing, "STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(billing, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    res = app_env.client.post(
        "/stripe/webhook",
        content=b'{"type":"customer.subscription.updated"}',
        headers={"stripe-signature": "t=1,v1=forged"},
    )
    assert res.status_code == 400


def test_the_report_carries_the_exclusions_it_was_run_with(app_env):
    """"Settings applied" has to be able to state what was left out. Without the
    value reaching the page there is no way to tell, after the fact, whether a
    missing subdomain was excluded on purpose or simply never found."""
    save(app_env, exclusions="web-staging, /careers")
    body = app_env.client.get("/crawl/r").text
    assert 'exclusions: "web-staging, /careers"' in body
    assert 'id="exclusions-pill"' in body
    assert 'id="exclusions-pill" style="display:none;"' not in body, (
        "the pill is always rendered — 'Excluded: none' is a different "
        "statement from the pill being absent"
    )


def test_a_run_with_no_exclusions_still_reports_that(app_env):
    save(app_env)
    body = app_env.client.get("/crawl/r").text
    assert "exclusions: null" in body
    assert 'id="exclusions-pill"' in body


# -------------------------------------------------------------------- promos

def test_both_the_shared_report_and_the_owners_pitch_wxrks(app_env):
    """The shared link reaches people who aren't users at all, which is the more
    valuable half — but the owner sees it too, or the one person checking this
    app daily never lays eyes on it."""
    save(app_env, total_words=662_000)
    run(app_env.db.set_run_public("r", True))

    for url in ("/share/r", "/crawl/r"):
        body = app_env.client.get(url, headers=counter_host(app_env)).text
        assert "wxrks.com" in body, url
        assert "words to translate" in body, url
    # Whether each is dismissible differs by audience — see the test below.


def test_the_markdown_surface_is_not_pitched_translation(app_env):
    save(app_env, total_words=662_000, surface="markdown")
    run(app_env.db.set_run_public("r", True))
    body = app_env.client.get("/share/r", headers=markdown_host(app_env)).text
    assert "words to translate" not in body


def test_the_pro_banner_needs_a_crawl_worth_complaining_about(app_env, monkeypatch):
    from app import billing

    monkeypatch.setattr(billing, "STRIPE_SECRET_KEY", "sk_test_x")

    save(app_env, duration_seconds=90, crawl_concurrency=4)
    assert "On Pro it would have taken" not in app_env.client.get("/crawl/r").text

    save(app_env, duration_seconds=3 * 3600, crawl_concurrency=4)
    body = app_env.client.get("/crawl/r").text
    assert "This crawl took ~3h" in body
    assert "/pricing" in body


def test_no_pro_banner_when_billing_is_off(app_env):
    save(app_env, duration_seconds=3 * 3600, crawl_concurrency=4)
    assert "On Pro it would have taken" not in app_env.client.get("/crawl/r").text


def test_the_pricing_page_is_readable_without_an_account(app_env, monkeypatch):
    """It exists to convince people to sign up; requiring them to sign up first
    made every link to it from outside the app a dead end."""
    from app import billing

    app_env.current["user"] = None
    monkeypatch.setattr(billing, "STRIPE_SECRET_KEY", "sk_test_x")
    res = app_env.client.get("/pricing")
    assert res.status_code == 200
    assert "Sign in to upgrade" in res.text


# -------------------------------------------------------------------- health

def test_the_health_page_is_admin_only(app_env, monkeypatch):
    """404 rather than 403, matching require_admin — a non-admin shouldn't be
    able to tell the page exists."""
    monkeypatch.setenv("ADMIN_EMAILS", "nobody@x.c")
    assert app_env.client.get("/admin/health").status_code == 404
    assert app_env.client.get("/admin/health.json").status_code == 404


def test_the_health_page_renders_for_an_admin(app_env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    res = app_env.client.get("/admin/health")
    assert res.status_code == 200
    for panel in ("Right now", "Disk", "Recent stops", "Disk by user"):
        assert panel in res.text


def test_the_health_page_survives_an_empty_database(app_env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    assert app_env.client.get("/admin/health").status_code == 200


def test_health_json_reports_the_real_limits(app_env, monkeypatch):
    from app import plans
    from app.crawler import MAX_CONCURRENT_CRAWLS

    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    body = app_env.client.get("/admin/health.json").json()
    assert body["live"]["max_concurrent_crawls"] == MAX_CONCURRENT_CRAWLS
    assert body["live"]["page_budget"] == plans.PAGE_BUDGET


def test_a_memory_kill_is_visible_afterwards(app_env, monkeypatch):
    """The whole point: before stop_kind, this was indistinguishable from
    somebody clicking Cancel."""
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    save(app_env, status="cancelled", stop_kind="memory",
         stopped_reason="Stopped automatically — too much memory.")
    body = app_env.client.get("/admin/health").text
    assert "memory" in body
    assert "too much memory" in body


# ----------------------------------------------------------------- user menu

ADMIN_MENU_ITEMS = ("Server health", "Running crawls", "All runs", "Estimate accuracy")


def test_the_menu_offers_admins_every_admin_page(app_env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    body = app_env.client.get("/").text
    for item in ADMIN_MENU_ITEMS:
        assert f">{item}<" in body


def test_the_menu_hides_the_admin_pages_from_everyone_else(app_env, monkeypatch):
    """The routes 404 for a non-admin anyway; the menu must not advertise that
    they exist, which is the same reason require_admin returns 404 not 403."""
    monkeypatch.setenv("ADMIN_EMAILS", "someone-else@x.c")
    body = app_env.client.get("/").text
    for item in ADMIN_MENU_ITEMS:
        assert f">{item}<" not in body


def test_the_menu_lists_only_pages_that_actually_open(app_env, monkeypatch):
    """It's built from the same is_admin() require_admin uses, so a listed page
    can never 404 — a menu that lies is worse than no menu."""
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    assert ">Server health<" in app_env.client.get("/").text
    for url in ("/admin/health", "/admin/jobs", "/admin/runs", "/admin/estimates"):
        assert app_env.client.get(url).status_code == 200


def test_every_admin_page_carries_the_menu_back_out(app_env, monkeypatch):
    """Admin pages had no chip at all, so they were a dead end."""
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    for url in ("/admin/health", "/admin/jobs", "/admin/runs", "/admin/estimates"):
        assert 'id="user-menu-list"' in app_env.client.get(url).text, url


def test_the_menu_follows_the_billing_state(app_env, monkeypatch):
    from app import billing

    assert ">Upgrade to Pro<" not in app_env.client.get("/").text, "nothing to sell yet"

    monkeypatch.setattr(billing, "STRIPE_SECRET_KEY", "sk_test_x")
    assert ">Upgrade to Pro<" in app_env.client.get("/").text

    app_env.current["user"] = app_env.pro
    body = app_env.client.get("/").text
    assert ">Manage billing<" in body
    assert ">Upgrade to Pro<" not in body


def test_preview_is_admin_only(app_env, monkeypatch):
    """Force-rendering banners is a diagnostic, not something a user can turn on
    for themselves."""
    monkeypatch.setenv("ADMIN_EMAILS", "someone-else@x.c")
    save(app_env, total_words=200, duration_seconds=0)
    body = app_env.client.get("/crawl/r?preview=promos").text
    assert "promo-preview" not in body
    assert "promo-pro" not in body


def test_preview_shows_an_admin_both_banners_with_stripe_off(app_env, monkeypatch):
    """The Pro banner is otherwise unviewable until billing is switched on."""
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    save(app_env, total_words=200, duration_seconds=0, crawl_concurrency=0)
    body = app_env.client.get("/crawl/r?preview=promos", headers=counter_host(app_env)).text
    assert "promo-pro" in body
    assert "promo-wxrks" in body
    assert body.count("promo-preview") == 2, "both must be labelled as previews"


def test_a_shared_report_cannot_be_previewed_into(app_env, monkeypatch):
    """No user on that route means is_admin is False, so the parameter is inert
    — worth pinning, because a leak there would be public."""
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    save(app_env, total_words=200, duration_seconds=0)
    run(app_env.db.set_run_public("r", True))
    assert "promo-preview" not in app_env.client.get("/share/r?preview=promos").text


def test_the_home_page_pitches_the_last_crawl_worth_translating(app_env):
    save(app_env, total_words=662_000)
    body = app_env.client.get("/", headers=counter_host(app_env)).text
    assert "wxrks.com" in body
    assert "Your last crawl of" in body


def test_the_home_page_says_nothing_before_a_first_crawl(app_env):
    body = app_env.client.get("/", headers=counter_host(app_env)).text
    assert "wxrks.com" not in body


def test_the_offer_cannot_be_dismissed(app_env):
    """Decided: the offer stays on every report, for the owner and for anyone
    they share it with. Nothing client-side hides it, which also means the one
    person checking whether it works can't accidentally silence it."""
    save(app_env, total_words=662_000)
    run(app_env.db.set_run_public("r", True))

    for url in ("/crawl/r", "/share/r", "/"):
        body = app_env.client.get(url, headers=counter_host(app_env)).text
        assert "promo-wxrks" in body, url
        assert "promo-dismiss" not in body, url
        assert "data-promo-dismiss" not in body, url


# ------------------------------------------------------------- report header

def test_the_report_header_carries_the_topbar(app_env):
    """The report page never had one, which is why the language menu and the
    account chip vanished the moment a crawl was opened."""
    save(app_env)
    html = app_env.client.get("/crawl/r", headers=counter_host(app_env)).text
    assert 'class="lang-menu"' in html
    assert "user-chip" in html


def test_exports_fold_into_one_menu_with_their_old_ids(app_env):
    """Option A: the ids inside are unchanged, so the existing JS handlers bind
    without knowing the markup moved."""
    save(app_env)
    html = app_env.client.get("/crawl/r", headers=counter_host(app_env)).text
    for el_id in ("export-csv-btn", "markdown-btn", "print-btn"):
        assert html.count(f'id="{el_id}"') == 1, el_id
    assert "action-menu-list" in html


def test_delete_and_recrawl_live_behind_the_dots(app_env):
    save(app_env)
    html = app_env.client.get("/crawl/r", headers=counter_host(app_env)).text
    assert html.count('id="delete-btn"') == 1, "a duplicate id binds the handler to the wrong one"
    assert 'id="recrawl-open-btn"' in html
    assert "run-disclosure-headless" in html, "the form stays full-width below, opened from the menu"


def test_a_shared_report_gets_no_owner_actions_but_keeps_its_exports(app_env):
    save(app_env)
    run(app_env.db.set_run_public("r", True))
    app_env.current["user"] = None
    del app_env.main.app.dependency_overrides[
        __import__("app.auth", fromlist=["get_current_user"]).get_current_user
    ]
    html = app_env.client.get("/share/r", headers=counter_host(app_env)).text
    assert "action-dots" not in html
    assert "recrawl-disclosure" not in html, "an anonymous re-crawl has no account to belong to"
    assert "export-csv-btn" in html, "exports are part of the report being shared"
    assert 'class="lang-menu"' in html


def test_the_menu_bindings_run_before_the_finished_report_returns(app_env):
    """initCrawlPage returns early for a finished report — and Re-crawl is
    precisely a finished-report action. Binding it after that return is how the
    menu item shipped doing nothing; this pins the order structurally."""
    js = (Path(__file__).resolve().parents[1] / "app/static/app.js").read_text()
    branch = js.index('if (opts.mode === "past" || opts.mode === "shared")')
    assert js.index('openFromMenu("recrawl-open-btn"') < branch
    assert js.index('openFromMenu("page-issues-open-btn"') < branch


def test_the_blocked_list_lives_behind_the_dots_too(app_env):
    save(app_env)
    html = app_env.client.get("/crawl/r", headers=counter_host(app_env)).text
    assert 'id="page-issues-open-btn"' in html
    assert html.count("run-disclosure-headless") == 2, "issues and re-crawl both open from the menu"
