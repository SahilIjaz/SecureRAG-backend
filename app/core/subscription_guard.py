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


# Statuses that may still use the product. `trial` and `active` are usable;
# `expired` and `cancelled` are not.
_USABLE_STATUSES = {SubscriptionStatus.trial, SubscriptionStatus.active}


def _is_expired_now(subscription: Subscription) -> bool:
    if subscription.expires_at is None:
        return False
    expires = subscription.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= expires


async def ensure_subscription_active(
    subscription: Optional[Subscription],
    db: AsyncSession,
) -> None:
    """Flip an elapsed subscription to `expired`, then raise 403 if the
    subscription can no longer be used. A missing subscription is treated as
    not-usable (fails closed). Safe to call on every request — the status
    write only happens on the transition."""
    if subscription is None:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="No active subscription. Please choose a plan to continue.",
        )

    if (
        subscription.status in _USABLE_STATUSES
        and _is_expired_now(subscription)
    ):
        subscription.status = SubscriptionStatus.expired
        await db.flush()

    if subscription.status not in _USABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Your subscription has ended. Renew your plan to keep using the chatbot.",
        )


async def is_subscription_usable(
    subscription: Optional[Subscription],
    db: AsyncSession,
) -> bool:
    """Soft, non-raising counterpart to ensure_subscription_active — flips an
    elapsed subscription to `expired`, then returns whether it may still be
    used. For surfaces (the public widget) that degrade gracefully to a
    fallback message instead of erroring the visitor's request."""
    if subscription is None:
        return False
    if subscription.status in _USABLE_STATUSES and _is_expired_now(subscription):
        subscription.status = SubscriptionStatus.expired
        await db.flush()
    return subscription.status in _USABLE_STATUSES


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
