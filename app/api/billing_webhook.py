"""
Stripe webhook — POST /api/billing/webhook.

Unauthenticated: Stripe calls this server-to-server and identifies itself
via a signature computed over the *raw* request body, verified against
STRIPE_WEBHOOK_SECRET. That's why this reads `await request.body()` instead
of taking a Pydantic model — a parsed-then-reserialized body would not match
the bytes Stripe signed.

Mounted directly in app/main.py (not through frontend_router's Depends
chain), same reasoning as the public widget sub-app: this endpoint has its
own trust model, not the dashboard's JWT auth.
"""

import logging

import stripe
from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal
from app.services import stripe_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["Billing — Stripe webhook"])

@router.post("/webhook", status_code=status.HTTP_200_OK)
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None, alias="stripe-signature"),
) -> dict:
    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, settings.STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        logger.warning("Rejected Stripe webhook: %s", e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid webhook signature.")

    event_type = event["type"]
    event_id = event.get("id")
    data = event["data"]["object"]

    # Own session, not Depends(get_db) — this route has no per-request auth
    # dependency chain to piggyback a session off of.
    async with AsyncSessionLocal() as db:
        # Idempotency: Stripe delivers at-least-once, so record the event id and
        # skip anything we've already processed. The INSERT is inside the same
        # transaction as the handler, so a crash mid-handling rolls back the
        # marker too and the retry re-processes cleanly.
        if event_id:
            from sqlalchemy import select
            from app.models.processed_stripe_event import ProcessedStripeEvent

            already = await db.execute(
                select(ProcessedStripeEvent.event_id).where(
                    ProcessedStripeEvent.event_id == event_id
                )
            )
            if already.scalar_one_or_none() is not None:
                logger.info("Skipping already-processed Stripe event %s", event_id)
                return {"received": True, "duplicate": True}
            db.add(ProcessedStripeEvent(event_id=event_id))

        try:
            if event_type in ("customer.subscription.created", "customer.subscription.updated"):
                await stripe_service.sync_subscription_from_stripe(data, db)
            elif event_type == "customer.subscription.deleted":
                await stripe_service.mark_subscription_cancelled(data, db)
            elif event_type == "setup_intent.succeeded":
                customer_id = data.get("customer")
                payment_method_id = data.get("payment_method")
                if customer_id and payment_method_id:
                    await stripe_service.set_default_payment_method(customer_id, payment_method_id)
                    # tenant_id isn't on the SetupIntent — look it up via the
                    # subscription row sync_payment_method_from_stripe needs.
                    from sqlalchemy import select
                    from app.models.subscription import Subscription

                    result = await db.execute(
                        select(Subscription).where(Subscription.stripe_customer_id == customer_id)
                    )
                    subscription = result.scalar_one_or_none()
                    if subscription is not None:
                        await stripe_service.sync_payment_method_from_stripe(
                            payment_method_id, subscription.tenant_id, db
                        )
            elif event_type == "invoice.payment_failed":
                customer_id = data.get("customer")
                if customer_id:
                    await stripe_service.notify_payment_failed(customer_id, db)

            await db.commit()
        except Exception:
            await db.rollback()
            raise

    return {"received": True}
