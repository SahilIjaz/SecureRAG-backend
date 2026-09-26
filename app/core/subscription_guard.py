"""
Subscription expiry enforcement.

Before this, a subscription's `expires_at` was never compared against the clock
and the `expired` status was written nowhere — so a lapsed trial or cancelled
plan kept working forever (bounded only by the monthly message quota, which
itself never reset). This module is the single place that:

  1. lazily flips a subscription to `expired` once `expires_at` has passed, and
  2. decides whether a subscription may still be *used* to consume the product.

`ensure_subscription_active()` is the shared primitive used by both the
authenticated dependency (dashboard routes) and the key/slug-resolved public
paths (widget, public chat), so the rule lives in exactly one place.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.frontend import helpers
from app.database import get_db
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.user import User
from app.services.auth_service import get_current_user


# Statuses that still have paid/trial time on the clock. `cancelled` is INCLUDED
# on purpose: cancelling sets cancel_at_period_end on Stripe, so the customer
# keeps access until expires_at (the trial end, or the end of the month they
# already paid for) — they are only actually blocked once that date passes. The
# single rule is therefore "usable while expires_at is in the future", checked
# below; only `expired` is unconditionally blocked.
_TIME_REMAINING_STATUSES = {
    SubscriptionStatus.trial,
    SubscriptionStatus.active,
    SubscriptionStatus.cancelled,
}


def _is_expired_now(subscription: Subscription) -> bool:
    if subscription.expires_at is None:
        return False
    expires = subscription.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= expires


async def _resolve_usable(subscription: Subscription, db: AsyncSession) -> bool:
    """Shared core: a subscription is usable while it still has time on the
    clock (trial/active/cancelled) AND that time hasn't run out. Once the period
    has elapsed, flip it to `expired` (so it stays blocked without re-checking
    the clock) and treat it as unusable."""
    if subscription.status not in _TIME_REMAINING_STATUSES:
        return False  # already expired (or any future terminal state)
    if _is_expired_now(subscription):
        # The trial/paid period is over — now it's genuinely blocked.
        if subscription.status != SubscriptionStatus.expired:
            subscription.status = SubscriptionStatus.expired
            await db.flush()
        return False
    # Still within the trial or paid period — cancelled-but-not-yet-expired
    # keeps working exactly like active, matching Stripe's own behaviour.
    return True


async def ensure_subscription_active(
    subscription: Optional[Subscription],
    db: AsyncSession,
) -> None:
    """Raise 402 if the subscription can no longer be used, flipping it to
    `expired` on the transition. A missing subscription fails closed. Safe to
    call on every request — the status write only happens on the transition."""
    if subscription is None:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="No active subscription. Please choose a plan to continue.",
        )

    if not await _resolve_usable(subscription, db):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Your subscription has ended. Renew your plan to keep using the chatbot.",
        )


async def is_subscription_usable(
    subscription: Optional[Subscription],
    db: AsyncSession,
) -> bool:
    """Soft, non-raising counterpart to ensure_subscription_active — for
    surfaces (the public widget) that degrade gracefully to a fallback message
    instead of erroring the visitor's request."""
    if subscription is None:
        return False
    return await _resolve_usable(subscription, db)


async def require_active_subscription(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    """FastAPI dependency for authenticated routes: loads the caller's
    subscription and enforces expiry. Returns the user so it can be used in
    place of `Depends(get_current_user)`."""
    subscription = await helpers.get_subscription(current_user.tenant_id, db)
    await ensure_subscription_active(subscription, db)
    return current_user
