from __future__ import annotations

import logging
import os
import uuid

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import db
from app.models import User
from app.templates import templates

logger = logging.getLogger(__name__)

router = APIRouter()

# The run copied into every new account so the first sign-in isn't an empty
# page. Matched by URL rather than run id so re-crawling the site refreshes
# what new users see, with no config change and no id to look up.
SAMPLE_RUN_URL = os.environ.get("SAMPLE_RUN_URL", "https://wxrks.com")


async def seed_sample_run(user_id: int) -> None:
    """Best effort: a new account signing in must never fail because the demo
    run is missing or the copy went wrong."""
    if not SAMPLE_RUN_URL:
        return
    try:
        template = await db.get_latest_run(db.normalize_url(SAMPLE_RUN_URL))
        if template is None:
            logger.info("No completed run for SAMPLE_RUN_URL=%s — new account starts empty", SAMPLE_RUN_URL)
            return
        await db.copy_run_to_user(template, user_id, uuid.uuid4().hex, as_sample=True)
    except Exception:
        logger.exception("Could not seed the sample run for user %s", user_id)

oauth = OAuth()
oauth.register(
    name="google",
    client_id=os.environ["GOOGLE_CLIENT_ID"],
    client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


async def get_current_user(request: Request) -> User | None:
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    if not hasattr(request.state, "user_cache"):
        user = await db.get_user(user_id)
        # Remember the language they are browsing in. Emails are sent later from
        # background tasks that have no request to read a prefix from, so this
        # is the only moment the preference is observable.
        lang = getattr(request.state, "lang", None)
        if user is not None and lang and lang != user.lang:
            try:
                await db.set_user_lang(user.id, lang)
                user = user.model_copy(update={"lang": lang})
            except Exception:
                logger.exception("Could not remember the language for user %s", user.id)
        request.state.user_cache = user
    return request.state.user_cache


async def require_user(request: Request) -> User:
    """Use for HTML pages: bounces an anonymous browser to /login."""
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


async def require_user_api(request: Request) -> User:
    """Use for JSON/SSE endpoints: a redirect isn't meaningful there."""
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def is_admin(user: User | None) -> bool:
    """Read at call time, not import time, so adding an address to ADMIN_EMAILS
    takes effect on restart rather than needing a rebuild. Shared with the
    templates so the menu offers exactly the pages the routes will allow —
    a menu that lists a page you then get 404 from is worse than no menu."""
    if user is None:
        return False
    admins = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
    return user.email.lower() in admins


def require_admin(user: User = Depends(require_user)) -> User:
    """404, not 403 — a non-admin shouldn't be able to tell this route
    exists at all, not just that they're forbidden from it."""
    if not is_admin(user):
        raise HTTPException(status_code=404)
    return user


@router.get("/login")
async def login_page(request: Request):
    error = request.query_params.get("error")
    return templates.TemplateResponse(request, "login.html", {"error": error})


@router.get("/auth/login")
async def auth_login(request: Request):
    redirect_uri = request.url_for("auth_callback")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception:
        return RedirectResponse(url="/login?error=Google+sign-in+failed", status_code=302)

    userinfo = token.get("userinfo")
    if userinfo is None:
        return RedirectResponse(url="/login?error=Could+not+read+Google+profile", status_code=302)

    user, created = await db.get_or_create_user(
        google_sub=userinfo["sub"],
        email=userinfo.get("email", ""),
        name=userinfo.get("name") or userinfo.get("email", "User"),
        picture=userinfo.get("picture"),
    )
    if created:
        await seed_sample_run(user.id)
    request.session["user_id"] = user.id
    return RedirectResponse(url="/", status_code=302)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)
