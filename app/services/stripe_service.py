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
from datetime import datetime, timezone

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

async def start_trial_subscription(
    tenant: Tenant,
    user: User,
    subscription: Subscription,
    plan_name: PlanName,
    db: AsyncSession,
) -> str:
    """
    Creates a Stripe subscription for `plan_name` with a 3-day trial and
    returns the SetupIntent client secret used to collect the card
    client-side (a trialing subscription has no invoice to pay yet, so
    Stripe's own pattern for "card required upfront" is a SetupIntent, not a
    PaymentIntent). Only stripe_customer_id/stripe_subscription_id/
    stripe_price_id are written here — plan_name/status/expires_at/quota are
    left untouched until the customer.subscription.created webhook confirms
    the trial actually started (see sync_subscription_from_stripe).
    """
    price_id = _plan_to_price()[plan_name]
    with perf_timing.timed("stripe_get_or_create_customer_total"):
        customer_id = await get_or_create_customer(tenant, user, subscription, db)

    # If the user already started (but never completed) a trial on a
    # different plan — e.g. picked Starter, backed up, picked Growth
    # instead — that earlier subscription is still sitting in Stripe as
    # `incomplete`/`trialing` with no confirmed payment method. Cancel it
    # rather than leaving it orphaned; it was never activated (no
    # customer.subscription.created webhook would have fired for it without
    # a confirmed SetupIntent), so this can't cancel a real paying sub.
    if subscription.stripe_subscription_id and subscription.stripe_price_id != price_id:
        try:
            with perf_timing.timed("stripe_api_cancel_stale_subscription"):
                await _run(stripe.Subscription.cancel, subscription.stripe_subscription_id)
        except stripe.error.StripeError as e:
            logger.warning("Could not cancel stale trial subscription %s: %s", subscription.stripe_subscription_id, e)

    with perf_timing.timed("stripe_api_subscription_create"):
        stripe_sub = await _run(
            stripe.Subscription.create,
            customer=customer_id,
            items=[{"price": price_id}],
            trial_period_days=TRIAL_DAYS,
            payment_behavior="default_incomplete",
            payment_settings={"save_default_payment_method": "on_subscription"},
            expand=["pending_setup_intent"],
        )

    subscription.stripe_subscription_id = stripe_sub.id
    subscription.stripe_price_id = price_id
    with perf_timing.timed("db_flush"):
        await db.flush()

    setup_intent = stripe_sub.pending_setup_intent
    if setup_intent is None or not setup_intent.client_secret:
        raise RuntimeError("Stripe did not return a SetupIntent for the trial subscription.")
    return setup_intent.client_secret

# ── Post-trial plan changes ─────────────────────────────────────────────────

async def change_subscription_plan(subscription: Subscription, plan_name: PlanName, db: AsyncSession) -> None:
    """Swaps the Stripe subscription's price (proration handled by Stripe) —
    no card involved, since a payment method is already on file from the
    trial. Updates the local row synchronously (unlike the trial flow) since
    this is a direct, server-initiated action with an immediate, certain
    outcome — the webhook still fires and reconciles idempotently."""
    if not subscription.stripe_subscription_id:
        raise ValueError("No Stripe subscription on file — complete checkout first.")

    price_id = _plan_to_price()[plan_name]
    stripe_sub = await _run(stripe.Subscription.retrieve, subscription.stripe_subscription_id)
    item_id = stripe_sub["items"]["data"][0]["id"]

    await _run(
        stripe.Subscription.modify,
        subscription.stripe_subscription_id,
        items=[{"id": item_id, "price": price_id}],
        proration_behavior="create_prorations",
    )

    subscription.plan_name = plan_name
    subscription.stripe_price_id = price_id
    # Every Stripe-backed plan is monthly-billed (no yearly option offered),
    # and the DB's chk_subscriptions_billing_cycle constraint requires this
    # to be set for any plan_name other than PlanName.free — which, post
    # repricing, is Starter, itself a paid plan too. Always set it.
    subscription.billing_cycle = BillingCycle.monthly
    subscription.status = SubscriptionStatus.active
    await _apply_quota(subscription.tenant_id, plan_name, db)
    await db.flush()

async def cancel_subscription(subscription: Subscription, db: AsyncSession) -> None:
    """cancel_at_period_end=True — access continues until the period ends,
    matching the existing frontend copy. Local status flips to `cancelled`
    immediately for display purposes even though Stripe itself still shows
    the subscription active until period end."""
    if subscription.stripe_subscription_id:
        await _run(
            stripe.Subscription.modify,
            subscription.stripe_subscription_id,
            cancel_at_period_end=True,
        )
    subscription.status = SubscriptionStatus.cancelled
    await db.flush()

async def create_card_update_setup_intent(subscription: Subscription) -> str:
    if not subscription.stripe_customer_id:
        raise ValueError("No Stripe customer on file — complete checkout first.")
    setup_intent = await _run(stripe.SetupIntent.create, customer=subscription.stripe_customer_id)
    return setup_intent.client_secret

# ── Webhook sync ─────────────────────────────────────────────────────────────

def _status_from_stripe(stripe_status: str) -> SubscriptionStatus:
    if stripe_status == "trialing":
        return SubscriptionStatus.trial
    if stripe_status in ("active", "past_due", "unpaid"):
        # past_due/unpaid still read as "active" for now — this is the hook
        # point for a future dunning/grace-period flag if payment retries
        # need surfacing beyond the invoice.payment_failed notification.
        return SubscriptionStatus.active
    return SubscriptionStatus.cancelled

async def sync_subscription_from_stripe(stripe_sub: dict, db: AsyncSession) -> None:
    result = await db.execute(
        select(Subscription).where(Subscription.stripe_subscription_id == stripe_sub["id"])
    )
    subscription = result.scalar_one_or_none()
    if subscription is None:
        logger.warning("Webhook for unknown Stripe subscription %s — ignoring.", stripe_sub["id"])
        return

    price_id = stripe_sub["items"]["data"][0]["price"]["id"]
    plan_name = _price_to_plan().get(price_id)
    if plan_name is not None:
        subscription.plan_name = plan_name
        subscription.stripe_price_id = price_id
        subscription.billing_cycle = BillingCycle.monthly  # see change_subscription_plan for why this is unconditional
        await _apply_quota(subscription.tenant_id, plan_name, db)

    subscription.status = _status_from_stripe(stripe_sub["status"])

    # As of Stripe API version 2026-07-29 (and some earlier ones), Subscription
    # no longer carries current_period_end at the top level — it moved to the
    # line item, since a subscription can now have multiple items on
    # different billing cycles. Fall back there; top-level check kept for
    # older API versions / a differently-configured Stripe account.
    period_end = stripe_sub.get("current_period_end") or stripe_sub["items"]["data"][0].get("current_period_end")
    if period_end:
        subscription.expires_at = datetime.fromtimestamp(period_end, tz=timezone.utc)

    await db.flush()

async def mark_subscription_cancelled(stripe_sub: dict, db: AsyncSession) -> None:
    result = await db.execute(
        select(Subscription).where(Subscription.stripe_subscription_id == stripe_sub["id"])
    )
    subscription = result.scalar_one_or_none()
    if subscription is None:
        return
    subscription.status = SubscriptionStatus.cancelled
    await db.flush()

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

async def create_wallet_topup_checkout_session(
    tenant, user: User, amount_usd: float, success_url: str, cancel_url: str, db: AsyncSession
) -> str:
    """
    One-time Stripe Checkout Session (payment mode, not subscription mode)
    that credits Tenant.balance_usd on completion — see
    billing_webhook.py's checkout.session.completed handler and
    app/services/wallet_service.py. Returns the Checkout Session URL to
    redirect the browser to.
    """
    if amount_usd < WALLET_TOPUP_MIN_USD:
        raise ValueError(f"Minimum top-up is ${WALLET_TOPUP_MIN_USD:.2f}.")

    from app.models.subscription import Subscription

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
        metadata={"tenant_id": str(tenant.id), "kind": "wallet_topup"},
        success_url=success_url,
        cancel_url=cancel_url,
    )
    return session.url

async def credit_wallet_from_checkout_session(session_data: dict, db: AsyncSession) -> None:
    """
    checkout.session.completed handler for a wallet top-up (mode="payment",
    metadata.kind="wallet_topup" — see create_wallet_topup_checkout_session).
    Must be idempotent: Stripe redelivers webhook events on any non-2xx
    response or plain network flakiness, and a naive handler would
    double-credit a tenant's balance on a retry. Guarded by
    stripe_payment_intent_id — if a WalletTransaction already exists for
    this payment intent, this is a no-op.
    """
    from decimal import Decimal

    from app.models.tenant import Tenant
    from app.models.wallet_transaction import WalletTransaction

    metadata = session_data.get("metadata") or {}
    if metadata.get("kind") != "wallet_topup":
        return  # a subscription checkout session, not ours to handle

    tenant_id = metadata.get("tenant_id")
    payment_intent_id = session_data.get("payment_intent")
    amount_total = session_data.get("amount_total")  # cents
    if not tenant_id or not payment_intent_id or amount_total is None:
        logger.warning("Wallet top-up checkout session missing required fields: %s", session_data.get("id"))
        return

    existing = await db.execute(
        select(WalletTransaction).where(WalletTransaction.stripe_payment_intent_id == payment_intent_id)
    )
    if existing.scalar_one_or_none() is not None:
        logger.info("Wallet top-up for payment_intent %s already recorded — skipping (webhook redelivery).", payment_intent_id)
        return

    amount_usd = Decimal(amount_total) / Decimal(100)
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

async def notify_payment_failed(customer_id: str, db: AsyncSession) -> None:
    from app.services.notification_service import notify_tenant

    result = await db.execute(
        select(Subscription).where(Subscription.stripe_customer_id == customer_id)
    )
    subscription = result.scalar_one_or_none()
    if subscription is None:
        return
    await notify_tenant(
        subscription.tenant_id,
        "plan_usage_warnings",
        "A payment on your subscription failed",
        "Update your payment method to avoid losing access when your trial or billing period ends.",
        "/dashboard/settings?tab=billing",
        db,
    )
