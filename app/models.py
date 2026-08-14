from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class User(BaseModel):
    id: int
    google_sub: str
    email: str
    name: str
    picture: str | None = None


class PageResult(BaseModel):
    url: str
    title: str = ""
    word_count: int = 0
    success: bool = True
    login_required: bool = False
    blocked_by_host: bool = False
    error: str | None = None


class RunRecord(BaseModel):
    id: str
    source_url: str
    user_id: int
    created_at: str
    status: str
    total_words: int
    page_count: int
    limit_reached: bool
    login_blocked_count: int = 0
    domain_scope: str = "all"
    language: str | None = None
    language_auto_detected: bool = False
    resume_state: dict | None = None
    is_public: bool = False
    # A copy of the demo run seeded into a new account, so the first sign-in
    # isn't an empty page. Owned and deletable like any other run.
    is_sample: bool = False
    # Saved page Markdown. Files live on disk keyed by run id, never in
    # pages_json — see app/markdown_store.py.
    capture_markdown: bool = False
    markdown_pages: int = 0
    markdown_bytes: int = 0
    markdown_state: str = "off"
    # Which front door this run came in through — see app/surfaces.py.
    surface: str = "counter"
    pages: list[PageResult]


class CrawlRequest(BaseModel):
    url: str
    domain_scope: Literal["all", "subdomain_only", "top_domain_only"] = "all"
    language: str | None = None
    force_recrawl: bool = False
    capture_markdown: bool = False


class ShareEmailRequest(BaseModel):
    email: str


class ShareToggleRequest(BaseModel):
    # Omitted means "flip whatever the current state is" — the switch in the
    # share dialog always sends an explicit state so it can't drift out of sync.
    is_public: bool | None = None
