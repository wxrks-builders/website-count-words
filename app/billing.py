"""Stripe checkout, customer portal, and the webhook that decides who is Pro.

The only thing being sold is crawl speed (see app/plans.py). Both hostnames
share one account — see app/surfaces.py — so one subscription covers both
products; a surface only changes how the pricing page words it.

Billing is optional. With STRIPE_SECRET_KEY unset every route here 404s and
everyone crawls at the free speed, the same way a blank MAILGUN_API_KEY turns
email off without breaking anything else.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app import db, plans, surfaces
from app.auth import get_current_user, require_user_api
from app.models import User
from app.templates import templates

logger = logging.getLogger(__name__)

router = APIRouter()

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")

# Subscription states that mean "this account has paid and should be fast".
# Anything else — past_due, canceled, unpaid, incomplete — falls back to free
# on its own, without needing a separate downgrade path.
_ACTIVE_STATUSES = ("active", "trialing")


def billing_enabled() -> bool:
    return bool(STRIPE_SECRET_KEY)


def _stripe():
    """Imported lazily so the package is only a dependency when billing is on,
    and so a missing install fails here rather than at startup."""
    import stripe

    stripe.api_key = STRIPE_SECRET_KEY
    return stripe


def _require_billing() -> None:
    # 404 rather than 503: with billing off these routes don't conceptually
    # exist, and the UI never links to them.
    if not billing_enabled():
        raise HTTPException(status_code=404)


def _origin(request: Request) -> str:
    """The surface this request came in through, so a checkout that starts on
    the Markdown host returns there rather than on the counter."""
    return surfaces.for_host(request.headers.get("host")).origin


async def _ensure_customer(user: User, request: Request) -> str:
    """The Stripe customer id for this account, creating one the first time.

    Stored so a returning subscriber reuses their customer rather than
    accumulating a new one per checkout, which would scatter their invoices.
    """
    if user.stripe_customer_id:
        return user.stripe_customer_id
    stripe = _stripe()
    customer = await run_in_threadpool(
        stripe.Customer.create,
        email=user.email,
        name=user.name,
        metadata={"user_id": str(user.id)},
    )
    await db.set_stripe_customer(user.id, customer.id)
    return customer.id


@router.get("/pricing")
async def pricing_page(request: Request, user: User | None = Depends(get_current_user)):
    """Public on purpose. Behind require_user this bounced anonymous visitors to
    /login, so every link to it from outside the app was a dead end — backwards
    for the one page whose job is to convince someone to sign up."""
    _require_billing()
    return templates.TemplateResponse(
        request,
        "pricing.html",
        {
            "user": user,
            "free_concurrency": plans.CONCURRENCY_FREE,
            "pro_concurrency": plans.CONCURRENCY_PRO,
            # The measured multiple, not the ratio of in-flight pages — see
            # plans.SCALING_EFFICIENCY for why those differ.
            "speedup": plans.advertised_speedup(),
        },
    )


@router.post("/billing/checkout")
async def create_checkout(request: Request, user: User = Depends(require_user_api)):
    _require_billing()
    if user.is_pro:
        # Already paying — send them to manage the subscription rather than
        # buying a second one.
        return JSONResponse({"already_pro": True})

    stripe = _stripe()
    customer_id = await _ensure_customer(user, request)
    origin = _origin(request)
    session = await run_in_threadpool(
        stripe.checkout.Session.create,
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        # Both carried so the webhook can find the account even if the customer
        # lookup somehow misses — client_reference_id survives on the session,
        # metadata survives onto the subscription.
        client_reference_id=str(user.id),
        subscription_data={"metadata": {"user_id": str(user.id)}},
        success_url=f"{origin}/?upgraded=1",
        cancel_url=f"{origin}/pricing",
    )
    return JSONResponse({"url": session.url})


@router.post("/billing/portal")
async def create_portal(request: Request, user: User = Depends(require_user_api)):
    _require_billing()
    if not user.stripe_customer_id:
        raise HTTPException(status_code=400, detail="No subscription to manage")
    stripe = _stripe()
    session = await run_in_threadpool(
        stripe.billing_portal.Session.create,
        customer=user.stripe_customer_id,
        return_url=_origin(request),
    )
    return JSONResponse({"url": session.url})


def _renews_at(subscription) -> str | None:
    period_end = subscription.get("current_period_end")
    if not period_end:
        return None
    return datetime.fromtimestamp(period_end, tz=timezone.utc).isoformat()


async def _user_for(subscription) -> User | None:
    """Which account a subscription belongs to.

    Customer id first, because that's the link that survives every event type.
    The metadata copied onto the subscription at checkout is the fallback, for
    the case where a customer row was replaced or never stored.
    """
    customer_id = subscription.get("customer")
    if customer_id:
        user = await db.get_user_by_stripe_customer(customer_id)
        if user is not None:
            return user
    user_id = (subscription.get("metadata") or {}).get("user_id")
    if user_id:
        user = await db.get_user(int(user_id))
        if user is not None and customer_id:
            # Backfill so the next event resolves on the fast path.
            await db.set_stripe_customer(user.id, customer_id)
        return user
    return None


async def _apply_subscription(subscription) -> None:
    """Writes plan state outright — never a delta.

    That is what makes this idempotent, which matters because Stripe retries
    deliveries and sends overlapping events for the same change. Replaying an
    event just rewrites the same row with the same values.
    """
    user = await _user_for(subscription)
    if user is None:
        logger.warning("Stripe subscription %s matched no account", subscription.get("id"))
        return
    status = subscription.get("status")
    await db.save_subscription_state(
        user_id=user.id,
        plan="pro" if status in _ACTIVE_STATUSES else "free",
        plan_status=status,
        stripe_subscription_id=subscription.get("id"),
        plan_renews_at=_renews_at(subscription),
    )


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """The only thing that grants Pro.

    Deliberately not driven by the checkout redirect: a browser can be pointed
    at success_url without ever having paid, so the plan is only ever written
    from a signature-verified event.
    """
    _require_billing()
    stripe = _stripe()

    # Raw bytes, not the parsed JSON — the signature is computed over the exact
    # body Stripe sent, so re-serializing it would never verify.
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    except Exception:
        # Includes both a bad signature and a malformed body. Never log the
        # payload — it carries customer details.
        logger.warning("Rejected a Stripe webhook with an invalid signature")
        raise HTTPException(status_code=400, detail="Invalid signature")

    kind = event["type"]
    obj = event["data"]["object"]

    if kind == "checkout.session.completed":
        # Links the customer to the account. The subscription events below are
        # what actually set the plan, and Stripe sends them for this checkout
        # too — so this handler only has to make the lookup work.
        user_id = obj.get("client_reference_id")
        customer_id = obj.get("customer")
        if user_id and customer_id:
            await db.set_stripe_customer(int(user_id), customer_id)
        subscription_id = obj.get("subscription")
        if subscription_id:
            subscription = await run_in_threadpool(stripe.Subscription.retrieve, subscription_id)
            await _apply_subscription(subscription)

    elif kind in ("customer.subscription.created", "customer.subscription.updated"):
        await _apply_subscription(obj)

    elif kind == "customer.subscription.deleted":
        user = await _user_for(obj)
        if user is not None:
            await db.save_subscription_state(
                user_id=user.id,
                plan="free",
                plan_status=obj.get("status") or "canceled",
                stripe_subscription_id=None,
                plan_renews_at=None,
            )

    # Everything else is acknowledged and ignored: returning an error would put
    # Stripe into a retry loop over events this app has no opinion about.
    return JSONResponse({"received": True})
