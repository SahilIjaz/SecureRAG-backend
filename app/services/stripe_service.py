"""
Stripe billing service — every `stripe` SDK call lives here so API route
files never import the package directly.

The Stripe Python SDK is synchronous (blocking network I/O), so every call
is routed through run_in_executor — same pattern already used for
hash_password() in api/frontend/settings.py — so a Stripe round trip never
blocks the event loop.

Subscription.plan_name stays on the app's original PlanName enum
(free/pro/pro_plus); this module only ever deals in that enum, never the
user-facing starter/growth/business ids — that translation happens once, in
api/frontend/helpers.py's FE_TO_BE_PLAN.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import stripe
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import perf_timing
from app.models.subscription import BillingCycle, PlanName, Subscription, SubscriptionStatus
from app.models.tenant import Tenant
from app.models.tenant_quota import TenantQuota
from app.models.tenant_settings import PaymentMethod
from app.models.user import User

logger = logging.getLogger(__name__)

TRIAL_DAYS = 3

def _configure() -> None:
    stripe.api_key = settings.STRIPE_SECRET_KEY

async def _run(fn, *args, **kwargs):
    """Run a blocking stripe.* SDK call off the event loop."""
    _configure()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

def _price_to_plan() -> dict[str, PlanName]:
    return {
        settings.STRIPE_PRICE_STARTER: PlanName.free,
        settings.STRIPE_PRICE_GROWTH: PlanName.pro,
        settings.STRIPE_PRICE_BUSINESS: PlanName.pro_plus,
    }

def _plan_to_price() -> dict[PlanName, str]:
    return {v: k for k, v in _price_to_plan().items()}

def plan_from_price_id(price_id: Optional[str]) -> Optional[PlanName]:
    """Public, server-authoritative price→plan resolution. Returns None if the
    price is unknown/empty. Use this to derive a tenant's plan from the Stripe
    price stored on their subscription rather than trusting any client input."""
    if not price_id:
        return None
    return _price_to_plan().get(price_id)

def _plan_quotas() -> dict[PlanName, dict]:
    # Imported lazily to avoid a circular import (auth_service imports
    # nothing from here, but keeping the dependency one-directional at
    # import time is simpler to reason about than risking a cycle later).
    from app.services.auth_service import PLAN_QUOTAS
    return PLAN_QUOTAS

async def _apply_quota(tenant_id, plan_name: PlanName, db: AsyncSession) -> None:
    result = await db.execute(select(TenantQuota).where(TenantQuota.tenant_id == tenant_id))
    quota = result.scalar_one_or_none()
    if quota is None:
        return
    quotas = _plan_quotas()[plan_name]
    quota.max_documents = quotas["max_documents"]
    quota.max_file_size_mb = quotas["max_file_size_mb"]
    quota.max_questions_per_month = quotas["max_questions_per_month"]
    quota.max_storage_mb = quotas["max_storage_mb"]

# ── Customer / trial subscription ───────────────────────────────────────────

async def get_or_create_customer(
    tenant: Tenant, user: User, subscription: Subscription, db: AsyncSession
) -> str:
    if subscription.stripe_customer_id:
        return subscription.stripe_customer_id
    with perf_timing.timed("stripe_api_customer_create"):
        customer = await _run(
            stripe.Customer.create,
            email=user.email,
            name=tenant.workspace_name,
            metadata={"tenant_id": str(tenant.id)},
        )
    subscription.stripe_customer_id = customer.id
    await db.flush()
    return customer.id

class BillingError(Exception):
    """A billing action that can't proceed — carries the HTTP status the
    route layer should answer with (e.g. 402 card required, 409 wrong state)."""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


_PLAN_RANK = {PlanName.free: 0, PlanName.pro: 1, PlanName.pro_plus: 2}


def _item_price_id(stripe_sub) -> Optional[str]:
    try:
        return stripe_sub["items"]["data"][0]["price"]["id"]
    except (KeyError, IndexError, TypeError):
        return None


def _card_on_file(stripe_sub) -> bool:
    """Whether the (customer-expanded) Stripe subscription can be charged."""
    if stripe_sub.get("default_payment_method"):
        return True
    customer = stripe_sub.get("customer")
    if customer is not None and not isinstance(customer, str):
        settings_ = customer.get("invoice_settings") or {}
        return bool(settings_.get("default_payment_method"))
    return False


async def start_trial_subscription(
    tenant: Tenant,
    user: User,
    subscription: Subscription,
    plan_name: PlanName,
    db: AsyncSession,
    skip_trial: bool = False,
) -> str:
    """
    Starts (or resumes / re-plans) the tenant's Stripe subscription and returns
    the client secret the frontend confirms with Stripe Elements:
      - ``seti_…`` SetupIntent — a first trial: the card is saved, nothing is
        charged until the trial ends;
      - ``pi_…`` PaymentIntent — no trial (already used, or skipped on
        purpose): the first invoice is charged immediately;
      - ``""`` — nothing to collect (a running subscription was resumed or
        re-planned on the card already on file).

    Only stripe_customer_id/stripe_subscription_id/stripe_price_id are written
    for a brand-new subscription — plan/status/expiry/quota land from
    sync_subscription_from_stripe once Stripe confirms it.
    """
    price_id = _plan_to_price()[plan_name]
    # The trial is one-time per tenant and optional: it applies only when it
    # has not been used AND the customer did not choose to skip it.
    is_first_trial = subscription.trial_used_at is None and not skip_trial
    with perf_timing.timed("stripe_get_or_create_customer_total"):
        customer_id = await get_or_create_customer(tenant, user, subscription, db)

    existing = None
    if subscription.stripe_subscription_id:
        try:
            with perf_timing.timed("stripe_api_subscription_retrieve"):
                existing = await _run(
                    stripe.Subscription.retrieve,
                    subscription.stripe_subscription_id,
                    expand=["pending_setup_intent", "latest_invoice.payment_intent"],
                )
        except stripe.error.StripeError as e:
            logger.warning("Could not retrieve subscription %s, creating a new one: %s", subscription.stripe_subscription_id, e)
    existing_status = existing.get("status") if existing is not None else None
    same_price = existing is not None and _item_price_id(existing) == price_id

    # A subscription that is still running but set to cancel at period end
    # (the customer cancelled, time is left): "resubscribe" resumes it, on the
    # new plan if they picked a different one. It must NOT be cancelled and
    # re-created — that would forfeit the time already paid for.
    if existing_status in ("trialing", "active") and existing.get("cancel_at_period_end"):
        await _run(stripe.Subscription.modify, existing["id"], cancel_at_period_end=False)
        if same_price:
            await sync_subscription_from_stripe(await _run(stripe.Subscription.retrieve, existing["id"]), db)
        else:
            await change_subscription_plan(subscription, plan_name, db, allow_in_trial=True)
        return ""

    # A live, paid subscription on a different plan: that is a plan change
    # (prorated on the card on file), never a second subscription.
    if existing_status == "active" and not same_price:
        await change_subscription_plan(subscription, plan_name, db)
        return ""

    # Mid-trial plan switch with a card already confirmed: swap the price,
    # the trial (and its single card) carries over.
    if existing_status == "trialing" and not same_price and await has_confirmed_payment_method(subscription):
        await change_subscription_plan(subscription, plan_name, db, allow_in_trial=True)
        return ""

    # A pending subscription whose trial-ness no longer matches the request
    # (user toggled "skip trial" on the same plan) must be replaced, not reused.
    if (
        same_price
        and existing_status in ("trialing", "incomplete")
        and bool(existing.get("trial_end")) != is_first_trial
    ):
        existing, existing_status = None, None
        try:
            await _run(stripe.Subscription.cancel, subscription.stripe_subscription_id)
        except stripe.error.StripeError as e:
            logger.warning("Could not cancel pending subscription %s: %s", subscription.stripe_subscription_id, e)

    # Retry of the SAME plan (card declined / bad ZIP / reload mid-step-3):
    # reuse the pending Stripe subscription instead of creating a second one.
    if same_price and existing_status in ("trialing", "incomplete", "active"):
        secret = _client_secret_from(existing)
        if secret is not None:
            return secret
        if existing.get("default_payment_method"):
            return ""  # card already on file — nothing left to collect

    # Whatever pending, never-activated subscription is still around is
    # orphaned from here on — cancel it rather than leaving it in Stripe.
    if existing is not None and existing_status in ("trialing", "incomplete"):
        try:
            with perf_timing.timed("stripe_api_cancel_stale_subscription"):
                await _run(stripe.Subscription.cancel, existing["id"])
        except stripe.error.StripeError as e:
            logger.warning("Could not cancel stale subscription %s: %s", existing["id"], e)

    create_kwargs = dict(
        customer=customer_id,
        items=[{"price": price_id}],
        payment_behavior="default_incomplete",
        payment_settings={"save_default_payment_method": "on_subscription"},
        expand=["pending_setup_intent", "latest_invoice.payment_intent"],
    )
    if is_first_trial:
        create_kwargs["trial_period_days"] = TRIAL_DAYS

    with perf_timing.timed("stripe_api_subscription_create"):
        stripe_sub = await _run(stripe.Subscription.create, **create_kwargs)

    subscription.stripe_subscription_id = stripe_sub.id
    subscription.stripe_price_id = price_id
    # Never leave a half-started subscription on a stale deadline: a trial
    # gets its real 3-day window, anything that must be paid for starts
    # expired until Stripe confirms payment (sync_subscription_from_stripe).
    now = datetime.now(timezone.utc)
    if is_first_trial:
        subscription.expires_at = now + timedelta(days=TRIAL_DAYS)
        subscription.status = SubscriptionStatus.trial
    else:
        subscription.expires_at = now
        subscription.status = SubscriptionStatus.expired
    with perf_timing.timed("db_flush"):
        await db.flush()

    secret = _client_secret_from(stripe_sub)
    if secret is not None:
        return secret
    raise RuntimeError("Stripe returned neither a SetupIntent nor a PaymentIntent client secret.")


def _client_secret_from(stripe_sub) -> Optional[str]:
    """First trial: a trialing subscription has no invoice yet, so Stripe hands
    back a SetupIntent to collect (not charge) the card. Repeat checkout: the
    subscription bills right away, so the client secret lives on the first
    invoice's PaymentIntent instead. Returns whichever exists, else None."""
    setup_intent = stripe_sub.get("pending_setup_intent") if hasattr(stripe_sub, "get") else getattr(stripe_sub, "pending_setup_intent", None)
    if setup_intent is not None and getattr(setup_intent, "client_secret", None):
        return setup_intent.client_secret
    invoice = stripe_sub.get("latest_invoice") if hasattr(stripe_sub, "get") else getattr(stripe_sub, "latest_invoice", None)
    payment_intent = getattr(invoice, "payment_intent", None) if invoice else None
    if payment_intent is not None and getattr(payment_intent, "client_secret", None):
        return payment_intent.client_secret
    return None


async def has_confirmed_payment_method(subscription: Subscription) -> Optional[bool]:
    """Whether Stripe has a card on file for this tenant's subscription.

    True  — default_payment_method set, or the pending SetupIntent succeeded.
    False — Stripe says there is definitely no card yet.
    None  — unknown (no Stripe subscription on the row, or Stripe unreachable);
            callers should not block on None.
    """
    if not subscription.stripe_subscription_id:
        return None
    try:
        stripe_sub = await _run(
            stripe.Subscription.retrieve,
            subscription.stripe_subscription_id,
            expand=["pending_setup_intent", "customer"],
        )
    except stripe.error.StripeError as e:
        logger.warning("Could not verify payment method for %s: %s", subscription.stripe_subscription_id, e)
        return None
    if stripe_sub.get("default_payment_method"):
        return True
    setup_intent = stripe_sub.get("pending_setup_intent")
    if setup_intent is not None and getattr(setup_intent, "status", None) == "succeeded":
        return True
    customer = stripe_sub.get("customer")
    invoice_settings = getattr(customer, "invoice_settings", None) if customer is not None and not isinstance(customer, str) else None
    if invoice_settings is not None and getattr(invoice_settings, "default_payment_method", None):
        return True
    return False

async def change_subscription_plan(
    subscription: Subscription, plan_name: PlanName, db: AsyncSession, *, allow_in_trial: bool = False
) -> None:
    """Moves the live Stripe subscription to another plan on the card already
    on file, then re-syncs the local row from Stripe (the source of truth).

      - Upgrade of a paid plan: the prorated difference is invoiced and
        charged *now* (``always_invoice`` + ``error_if_incomplete``) — a
        declined card fails the whole change and nothing is granted. Before,
        the proration was merely queued for the next invoice, so a customer
        could upgrade, use the bigger plan and downgrade before ever paying.
      - Downgrade: effective immediately, the unused difference is credited
        to the next invoice.
      - During a trial nothing is charged; the trial carries over.
    Raises BillingError with the HTTP status the caller should answer with.
    """
    if not subscription.stripe_subscription_id:
        raise BillingError("No Stripe subscription on file — complete checkout first.", 409)

    price_id = _plan_to_price()[plan_name]
    stripe_sub = await _run(
        stripe.Subscription.retrieve, subscription.stripe_subscription_id, expand=["customer"]
    )
    if stripe_sub["status"] not in ("trialing", "active"):
        raise BillingError("Your subscription isn't active. Subscribe again to choose a plan.", 409)
    if stripe_sub.get("cancel_at_period_end"):
        raise BillingError("Your plan is set to cancel. Resubscribe to resume it before changing plans.", 409)
    if stripe_sub["status"] == "trialing" and not allow_in_trial:
        # Like real products: the trial is on the plan you started it with.
        # To use a different plan you start it for real (ends the trial).
        raise BillingError("Plan changes are available after your free trial. Start your plan now to switch.", 409)

    current_price = _item_price_id(stripe_sub)
    if current_price == price_id:
        await sync_subscription_from_stripe(stripe_sub, db)
        return
    if not _card_on_file(stripe_sub):
        raise BillingError("Add a payment card before changing your plan.", 402)

    current_plan = _price_to_plan().get(current_price, subscription.plan_name)
    is_paid_upgrade = stripe_sub["status"] == "active" and _PLAN_RANK[plan_name] > _PLAN_RANK[current_plan]
    kwargs: dict = dict(
        items=[{"id": stripe_sub["items"]["data"][0]["id"], "price": price_id}],
        proration_behavior="always_invoice" if is_paid_upgrade else "create_prorations",
    )
    if is_paid_upgrade:
        kwargs["payment_behavior"] = "error_if_incomplete"
    try:
        updated = await _run(stripe.Subscription.modify, subscription.stripe_subscription_id, **kwargs)
    except stripe.error.CardError as e:
        raise BillingError(f"Your card was declined: {e.user_message or 'please use another card'}", 402)
    except stripe.error.StripeError as e:
        logger.warning("Plan change failed for %s: %s", subscription.stripe_subscription_id, e)
        raise BillingError(getattr(e, "user_message", None) or "Could not change your plan — please try again.", 402)

    await sync_subscription_from_stripe(updated, db)

async def start_plan_now(subscription: Subscription, plan_name: PlanName, db: AsyncSession) -> None:
    """Ends the free trial immediately and starts the (chosen) plan for real:
    the first invoice is charged to the card on file right now, and a declined
    card fails the whole action so the trial stays untouched."""
    if not subscription.stripe_subscription_id:
        raise BillingError("No Stripe subscription on file — complete checkout first.", 409)
    stripe_sub = await _run(stripe.Subscription.retrieve, subscription.stripe_subscription_id, expand=["customer"])
    if stripe_sub["status"] != "trialing":
        raise BillingError("You're not on a free trial.", 409)
    if stripe_sub.get("cancel_at_period_end"):
        raise BillingError("Your plan is set to cancel. Resubscribe to resume it first.", 409)
    if not _card_on_file(stripe_sub):
        raise BillingError("Add a payment card to start your plan.", 402)
    kwargs: dict = dict(trial_end="now", proration_behavior="none", payment_behavior="error_if_incomplete")
    price_id = _plan_to_price()[plan_name]
    if _item_price_id(stripe_sub) != price_id:
        kwargs["items"] = [{"id": stripe_sub["items"]["data"][0]["id"], "price": price_id}]
    try:
        updated = await _run(stripe.Subscription.modify, subscription.stripe_subscription_id, **kwargs)
    except stripe.error.CardError as e:
        raise BillingError(f"Your card was declined: {e.user_message or 'please use another card'}", 402)
    except stripe.error.StripeError as e:
        logger.warning("Start-now failed for %s: %s", subscription.stripe_subscription_id, e)
        raise BillingError(getattr(e, "user_message", None) or "Could not start your plan — please try again.", 402)
    await sync_subscription_from_stripe(updated, db)

async def cancel_subscription(subscription: Subscription, db: AsyncSession) -> None:
    """cancel_at_period_end=True — the customer keeps what they already paid
    for (access continues until the period ends) and is never charged again.
    Works during a trial too (the trial simply never converts)."""
    if subscription.stripe_subscription_id:
        updated = await _run(
            stripe.Subscription.modify,
            subscription.stripe_subscription_id,
            cancel_at_period_end=True,
        )
        await sync_subscription_from_stripe(updated, db)
        return
    subscription.status = SubscriptionStatus.cancelled
    await db.flush()

async def create_card_update_setup_intent(subscription: Subscription) -> str:
    if not subscription.stripe_customer_id:
        raise ValueError("No Stripe customer on file — complete checkout first.")
    setup_intent = await _run(
        stripe.SetupIntent.create, customer=subscription.stripe_customer_id, usage="off_session"
    )
    return setup_intent.client_secret

async def confirm_card_setup(subscription: Subscription, setup_intent_id: str, db: AsyncSession) -> None:
    """Makes the card the customer just confirmed the default for the
    customer AND the subscription, and syncs the brand/last4 shown in the UI.
    Done here, not only in the setup_intent.succeeded webhook, so a new card
    works even where webhooks aren't reaching the server (local dev)."""
    si = await _run(stripe.SetupIntent.retrieve, setup_intent_id)
    if si.get("customer") != subscription.stripe_customer_id or si.get("status") != "succeeded":
        raise BillingError("That card setup could not be verified.", 400)
    pm_id = si.get("payment_method")
    if not pm_id:
        raise BillingError("That card setup could not be verified.", 400)
    await set_default_payment_method(subscription.stripe_customer_id, pm_id)
    if subscription.stripe_subscription_id:
        try:
            await _run(stripe.Subscription.modify, subscription.stripe_subscription_id, default_payment_method=pm_id)
        except stripe.error.StripeError as e:
            logger.warning("Could not set subscription default card: %s", e)
    await sync_payment_method_from_stripe(pm_id, subscription.tenant_id, db)

async def ensure_card_synced(subscription: Subscription, db: AsyncSession) -> None:
    """Fills the 'card on file' row from Stripe when the webhook that normally
    does it hasn't arrived, so the UI never claims 'No card' for a paid plan."""
    if not subscription.stripe_customer_id:
        return
    record = await _get_or_create_payment_method_row(subscription.tenant_id, db)
    if record.last4 and record.last4 != "0000":
        return
    try:
        customer = await _run(stripe.Customer.retrieve, subscription.stripe_customer_id)
        pm_id = (customer.get("invoice_settings") or {}).get("default_payment_method")
        if not pm_id and subscription.stripe_subscription_id:
            sub = await _run(stripe.Subscription.retrieve, subscription.stripe_subscription_id)
            pm_id = sub.get("default_payment_method")
        if pm_id:
            await sync_payment_method_from_stripe(pm_id, subscription.tenant_id, db)
    except stripe.error.StripeError as e:
        logger.warning("Could not sync card for %s: %s", subscription.stripe_customer_id, e)

# ── Webhook sync ─────────────────────────────────────────────────────────────

def _access_from_stripe(stripe_sub) -> tuple[SubscriptionStatus, bool]:
    """(local status, whether the customer currently has paid-for access).

    Only a trialing or active Stripe subscription grants access. past_due /
    unpaid (a renewal charge failed — Stripe has already rolled the period
    forward, so trusting its dates would hand out a free month), incomplete
    (first payment never completed), paused and canceled do NOT.
    Before, those mapped to 'active'/'cancelled' and kept the plan, its quota
    and a future expiry — i.e. a declined or abandoned payment still unlocked
    the product."""
    s = stripe_sub["status"]
    if s in ("trialing", "active"):
        if stripe_sub.get("cancel_at_period_end"):
            return SubscriptionStatus.cancelled, True  # access until the paid period ends
        return (SubscriptionStatus.trial if s == "trialing" else SubscriptionStatus.active), True
    return SubscriptionStatus.expired, False

async def sync_subscription_from_stripe(stripe_sub: dict, db: AsyncSession) -> None:
    result = await db.execute(
        select(Subscription).where(Subscription.stripe_subscription_id == stripe_sub["id"])
    )
    subscription = result.scalar_one_or_none()
    if subscription is None:
        logger.warning("Webhook for unknown Stripe subscription %s — ignoring.", stripe_sub["id"])
        return

    new_status, has_access = _access_from_stripe(stripe_sub)
    price_id = _item_price_id(stripe_sub)
    plan_name = _price_to_plan().get(price_id)
    if has_access and plan_name is not None:
        subscription.plan_name = plan_name
        subscription.stripe_price_id = price_id
        subscription.billing_cycle = BillingCycle.monthly  # every Stripe-backed plan bills monthly
        await _apply_quota(subscription.tenant_id, plan_name, db)

    previous = subscription.status
    subscription.status = new_status
    # The trial is "used" only once Stripe confirms it actually started.
    if stripe_sub["status"] in ("trialing", "active") and subscription.trial_used_at is None:
        subscription.trial_used_at = datetime.now(timezone.utc)

    # As of Stripe API version 2026-07-29 (and some earlier ones), Subscription
    # no longer carries current_period_end at the top level — it moved to the
    # line item. Fall back there; top-level kept for older API versions.
    period_end = stripe_sub.get("current_period_end") or stripe_sub["items"]["data"][0].get("current_period_end")
    if has_access:
        if period_end:
            subscription.expires_at = datetime.fromtimestamp(period_end, tz=timezone.utc)
    else:
        # No paid access: close the window now.
        subscription.expires_at = datetime.now(timezone.utc)

    await db.flush()
    if previous != new_status and new_status == SubscriptionStatus.expired:
        await notify_billing(
            subscription.tenant_id, "ended",
            "Your subscription is not active",
            "Your plan is no longer active, so your chatbot and dashboard are paused. "
            "Renew in Billing to restore access.",
            db,
        )

async def mark_subscription_cancelled(stripe_sub: dict, db: AsyncSession) -> None:
    result = await db.execute(
        select(Subscription).where(Subscription.stripe_subscription_id == stripe_sub["id"])
    )
    subscription = result.scalar_one_or_none()
    if subscription is None:
        return
    # The Stripe subscription is gone (period ended, or cancelled outright):
    # access ends now and the paid entitlements drop back to the free tier.
    subscription.status = SubscriptionStatus.expired
    now = datetime.now(timezone.utc)
    exp = subscription.expires_at
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    subscription.expires_at = min(now, exp) if exp is not None else now
    subscription.plan_name = PlanName.free
    await _apply_quota(subscription.tenant_id, PlanName.free, db)
    await db.flush()
    await notify_billing(
        subscription.tenant_id, "ended",
        "Your subscription has ended",
        "Your plan has ended, so your chatbot and dashboard are paused. Subscribe again in Billing to restore access.",
        db,
    )

# Throttle so a flood of requests from a lapsed tenant can't hammer Stripe.
_last_refresh: dict[str, float] = {}

async def refresh_from_stripe(subscription: Subscription, db: AsyncSession, *, force: bool = False, min_interval: float = 15.0) -> bool:
    """Pulls the authoritative subscription state from Stripe into the local
    row. This is what makes billing correct even when webhooks aren't
    delivered (local dev, a misconfigured endpoint, an outage): a renewal that
    extended the period is picked up, a failed or cancelled one is caught.
    Returns True when a sync actually ran."""
    sid = subscription.stripe_subscription_id
    if not sid:
        return False
    now = time.monotonic()
    if not force and now - _last_refresh.get(sid, -1e9) < min_interval:
        return False
    _last_refresh[sid] = now
    try:
        stripe_sub = await _run(stripe.Subscription.retrieve, sid)
    except stripe.error.StripeError as e:
        logger.warning("Could not refresh subscription %s from Stripe: %s", sid, e)
        return False
    await sync_subscription_from_stripe(stripe_sub, db)
    return True

async def _get_or_create_payment_method_row(tenant_id, db: AsyncSession) -> PaymentMethod:
    result = await db.execute(select(PaymentMethod).where(PaymentMethod.tenant_id == tenant_id))
    record = result.scalar_one_or_none()
    if record is None:
        record = PaymentMethod(tenant_id=tenant_id)
        db.add(record)
        await db.flush()
    return record

async def sync_payment_method_from_stripe(payment_method_id: str, tenant_id, db: AsyncSession) -> None:
    pm = await _run(stripe.PaymentMethod.retrieve, payment_method_id)
    card = pm.get("card")
    if not card:
        return
    record = await _get_or_create_payment_method_row(tenant_id, db)
    record.brand = (card.get("brand") or "card").upper()
    record.last4 = card.get("last4", "0000")
    record.expiry = f"{card.get('exp_month', 0):02d}/{str(card.get('exp_year', 0))[-2:]}"
    await db.flush()

async def set_default_payment_method(customer_id: str, payment_method_id: str) -> None:
    await _run(
        stripe.Customer.modify,
        customer_id,
        invoice_settings={"default_payment_method": payment_method_id},
    )

# ── Wallet top-up (one-time payment, separate from the subscription flow above) ──

WALLET_TOPUP_MIN_USD = 0.50  # Stripe's own real minimum charge amount
WALLET_TOPUP_MAX_USD = 1000.0

def _validate_topup_amount(amount_usd: float) -> None:
    if amount_usd < WALLET_TOPUP_MIN_USD:
        raise ValueError(f"Minimum top-up is ${WALLET_TOPUP_MIN_USD:.2f}.")
    if amount_usd > WALLET_TOPUP_MAX_USD:
        raise ValueError(f"Maximum top-up is ${WALLET_TOPUP_MAX_USD:,.0f}.")

async def create_wallet_topup_intent(tenant, user: User, amount_usd: float, db: AsyncSession) -> dict:
    """In-app wallet top-up: a PaymentIntent the dashboard confirms with
    Stripe Elements (so the dialog says 'Pay $X', shows saved-card/new-card
    options, and never leaves the page). Credited by confirm_wallet_topup()
    (called right after the browser confirms) and, redundantly, by the
    payment_intent.succeeded webhook — both idempotent on the PaymentIntent id."""
    _validate_topup_amount(amount_usd)
    result = await db.execute(select(Subscription).where(Subscription.tenant_id == tenant.id))
    subscription = result.scalar_one_or_none()
    customer_id = None
    if subscription is not None:
        customer_id = await get_or_create_customer(tenant, user, subscription, db)
    intent = await _run(
        stripe.PaymentIntent.create,
        amount=round(amount_usd * 100),
        currency="usd",
        customer=customer_id,
        receipt_email=user.email if customer_id is None else None,
        automatic_payment_methods={"enabled": True},
        description="Nexus wallet top-up",
        metadata={"tenant_id": str(tenant.id), "kind": "wallet_topup"},
    )
    return {"clientSecret": intent.client_secret, "paymentIntentId": intent.id}

async def create_wallet_topup_checkout_session(
    tenant, user: User, amount_usd: float, success_url: str, cancel_url: str, db: AsyncSession
) -> str:
    """Hosted-page variant kept for compatibility (credited by the
    checkout.session.completed webhook). The dashboard uses the in-app
    PaymentIntent flow above."""
    _validate_topup_amount(amount_usd)
    result = await db.execute(select(Subscription).where(Subscription.tenant_id == tenant.id))
    subscription = result.scalar_one_or_none()
    customer_id = None
    if subscription is not None:
        customer_id = await get_or_create_customer(tenant, user, subscription, db)

    session = await _run(
        stripe.checkout.Session.create,
        mode="payment",
        customer=customer_id,
        customer_email=user.email if customer_id is None else None,
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Nexus wallet top-up"},
                "unit_amount": round(amount_usd * 100),
            },
            "quantity": 1,
        }],
        payment_intent_data={"metadata": {"tenant_id": str(tenant.id), "kind": "wallet_topup"}},
        metadata={"tenant_id": str(tenant.id), "kind": "wallet_topup"},
        success_url=success_url,
        cancel_url=cancel_url,
    )
    return session.url

async def _credit_wallet(tenant_id, payment_intent_id: str, amount_cents: int, db: AsyncSession) -> bool:
    """Credits a top-up exactly once per PaymentIntent. The tenant row is
    locked first so the browser-confirm call and the webhook racing each other
    can't both pass the 'already credited?' check and double-credit."""
    from decimal import Decimal

    from app.models.tenant import Tenant
    from app.models.wallet_transaction import WalletTransaction

    await db.execute(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
    existing = await db.execute(
        select(WalletTransaction.id).where(WalletTransaction.stripe_payment_intent_id == payment_intent_id).limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        logger.info("Wallet top-up for payment_intent %s already recorded — skipping.", payment_intent_id)
        return False

    amount_usd = Decimal(amount_cents) / Decimal(100)
    result = await db.execute(
        update(Tenant).where(Tenant.id == tenant_id)
        .values(balance_usd=Tenant.balance_usd + amount_usd, wallet_low_balance_warned=False)
        .returning(Tenant.balance_usd)
    )
    new_balance = result.scalar_one_or_none()
    db.add(WalletTransaction(
        tenant_id=tenant_id, type="topup", amount_usd=amount_usd,
        balance_after=new_balance, stripe_payment_intent_id=payment_intent_id,
    ))
    await db.flush()
    await notify_billing(
        tenant_id, f"topup-{payment_intent_id[-8:]}",
        "Funds added to your wallet",
        f"${amount_usd:.2f} was added. New balance: ${new_balance:.2f}." if new_balance is not None else f"${amount_usd:.2f} was added.",
        db,
    )
    return True

async def confirm_wallet_topup(payment_intent_id: str, tenant_id, db: AsyncSession) -> bool:
    """Called by the dashboard right after Stripe confirms the card. The
    PaymentIntent is re-read from Stripe and must belong to this tenant and be
    paid — the client can't claim funds it didn't pay for."""
    intent = await _run(stripe.PaymentIntent.retrieve, payment_intent_id)
    metadata = intent.get("metadata") or {}
    if metadata.get("kind") != "wallet_topup" or metadata.get("tenant_id") != str(tenant_id):
        raise BillingError("That payment doesn't belong to this workspace.", 403)
    if intent.get("status") != "succeeded":
        raise BillingError("The payment hasn't completed yet.", 409)
    return await _credit_wallet(tenant_id, payment_intent_id, int(intent["amount_received"] or intent["amount"]), db)

async def credit_wallet_from_payment_intent(intent: dict, db: AsyncSession) -> None:
    """payment_intent.succeeded webhook for wallet top-ups."""
    metadata = intent.get("metadata") or {}
    if metadata.get("kind") != "wallet_topup" or not metadata.get("tenant_id"):
        return
    await _credit_wallet(metadata["tenant_id"], intent["id"], int(intent.get("amount_received") or intent["amount"]), db)

async def credit_wallet_from_checkout_session(session_data: dict, db: AsyncSession) -> None:
    """checkout.session.completed handler for a hosted wallet top-up
    (mode="payment", metadata.kind="wallet_topup"). Idempotent via
    _credit_wallet — Stripe redelivers webhook events."""
    metadata = session_data.get("metadata") or {}
    if metadata.get("kind") != "wallet_topup":
        return  # not ours
    tenant_id = metadata.get("tenant_id")
    payment_intent_id = session_data.get("payment_intent")
    amount_total = session_data.get("amount_total")  # cents
    if not tenant_id or not payment_intent_id or amount_total is None:
        logger.warning("Wallet top-up checkout session missing required fields: %s", session_data.get("id"))
        return
    if session_data.get("payment_status") not in (None, "paid"):
        return
    await _credit_wallet(tenant_id, payment_intent_id, int(amount_total), db)

# ── Billing notifications ───────────────────────────────────────────────────
# Distinct `notice` values give each event its own link, because notify_tenant
# de-duplicates unread notifications by (type, link) — one shared link would
# let an unread "payment failed" swallow the next "trial ending" warning.

async def notify_billing(tenant_id, notice: str, title: str, body: str, db: AsyncSession) -> None:
    from app.services.notification_service import notify_tenant

    await notify_tenant(
        tenant_id, "plan_usage_warnings", title, body,
        f"/dashboard/settings?tab=billing&notice={notice}", db,
    )

async def _subscription_by_customer(customer_id: str, db: AsyncSession) -> Optional[Subscription]:
    result = await db.execute(select(Subscription).where(Subscription.stripe_customer_id == customer_id))
    return result.scalar_one_or_none()

def _fmt_date(ts) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d, %Y")

def _plan_price_label(subscription: Subscription) -> str:
    from app.api.frontend import helpers
    return helpers.PLAN_PRICE_LABELS[helpers.fe_plan_for_subscription(subscription)]

async def notify_payment_failed(customer_id: str, db: AsyncSession) -> None:
    subscription = await _subscription_by_customer(customer_id, db)
    if subscription is None:
        return
    await notify_billing(
        subscription.tenant_id, "payment-failed",
        "Your payment failed",
        "We couldn't charge your card, so your plan is paused. Update your payment method in Billing to restore access.",
        db,
    )

async def notify_trial_ending(stripe_sub: dict, db: AsyncSession) -> None:
    result = await db.execute(select(Subscription).where(Subscription.stripe_subscription_id == stripe_sub["id"]))
    subscription = result.scalar_one_or_none()
    if subscription is None or stripe_sub.get("cancel_at_period_end"):
        return
    when = _fmt_date(stripe_sub["trial_end"]) if stripe_sub.get("trial_end") else "soon"
    await notify_billing(
        subscription.tenant_id, "trial-ending",
        f"Your free trial ends {when}",
        f"{_plan_price_label(subscription)} will be charged automatically to your card on {when}. "
        "Cancel in Billing before then if you don't want to continue.",
        db,
    )

async def notify_payment_succeeded(invoice: dict, db: AsyncSession) -> None:
    paid = invoice.get("amount_paid") or 0
    customer_id = invoice.get("customer")
    if paid <= 0 or not customer_id:
        return  # the $0 invoice a trial starts with
    subscription = await _subscription_by_customer(customer_id, db)
    if subscription is None:
        return
    await notify_billing(
        subscription.tenant_id, f"paid-{str(invoice.get('id'))[-8:]}",
        "Payment received",
        f"We charged ${paid / 100:.2f} for your plan. Thank you! It renews automatically each month — cancel any time in Billing.",
        db,
    )

async def notify_upcoming_renewal(invoice: dict, db: AsyncSession) -> None:
    customer_id = invoice.get("customer")
    subscription = await _subscription_by_customer(customer_id, db) if customer_id else None
    if subscription is None:
        return
    due = invoice.get("amount_due") or 0
    when = _fmt_date(invoice["next_payment_attempt"]) if invoice.get("next_payment_attempt") else "soon"
    await notify_billing(
        subscription.tenant_id, "renewal-upcoming",
        f"Your plan renews {when}",
        f"${due / 100:.2f} will be charged to your card on {when}. Cancel in Billing before then to stop renewal.",
        db,
    )
