"""Billing rules that must never regress: what Stripe states grant access, when a
local subscription row counts as usable, which routes the subscription gate
covers, and that the wallet bills models it has no exact price for."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from fastapi.routing import APIRoute

from app.core.subscription_guard import _currently_usable, require_active_subscription
from app.main import app
from app.models.subscription import SubscriptionStatus as S
from app.services import stripe_service
from app.services.llm import pricing


def _sub(status, expires_in_days=None, stripe_id="sub_1"):
    expires = None if expires_in_days is None else datetime.now(timezone.utc) + timedelta(days=expires_in_days)
    return SimpleNamespace(status=status, expires_at=expires, stripe_subscription_id=stripe_id)


def test_only_trialing_or_active_stripe_states_grant_access():
    cases = {
        "trialing": (S.trial, True), "active": (S.active, True),
        "past_due": (S.expired, False), "unpaid": (S.expired, False),
        "incomplete": (S.expired, False), "incomplete_expired": (S.expired, False),
        "canceled": (S.expired, False), "paused": (S.expired, False),
    }
    for stripe_status, expected in cases.items():
        assert stripe_service._access_from_stripe({"status": stripe_status}) == expected, stripe_status


def test_cancel_at_period_end_keeps_access_but_shows_cancelled():
    for st in ("trialing", "active"):
        assert stripe_service._access_from_stripe({"status": st, "cancel_at_period_end": True}) == (S.cancelled, True)


def test_local_row_usability_fails_closed():
    assert _currently_usable(_sub(S.active, 20))
    assert _currently_usable(_sub(S.trial, 2))
    assert _currently_usable(_sub(S.cancelled, 5))          # cancelled but paid-through
    assert not _currently_usable(_sub(S.active, -1))         # period over
    assert not _currently_usable(_sub(S.cancelled, -1))
    assert not _currently_usable(_sub(S.expired, 30))        # expired is never usable
    # No deadline on a Stripe-backed / pre-checkout row used to mean "forever".
    assert not _currently_usable(_sub(S.trial, None, stripe_id="sub_1"))
    assert not _currently_usable(_sub(S.trial, None, stripe_id=None))
    assert not _currently_usable(_sub(S.cancelled, None, stripe_id="sub_1"))
    # Legacy plan never attached to Stripe keeps working.
    assert _currently_usable(_sub(S.active, None, stripe_id=None))


def _walk(dependant):
    for d in dependant.dependencies:
        yield d.call
        yield from _walk(d)


def test_product_routes_are_behind_the_subscription_gate():
    gated_prefixes = (
        "/api/chatbot", "/api/knowledge", "/api/dashboard", "/api/conversations",
        "/api/model-options", "/api/settings/model", "/api/audit",
    )
    ungated_ok = ("/api/settings", "/api/team", "/api/user", "/api/auth", "/api/onboarding", "/api/notifications", "/api/presence")
    missing = []
    for r in app.routes:
        if not isinstance(r, APIRoute) or not r.path.startswith("/api"):
            continue
        has_gate = require_active_subscription in list(_walk(r.dependant))
        if r.path.startswith(gated_prefixes) and not has_gate:
            missing.append(r.path)
        if r.path.startswith("/api/settings/billing") or r.path.startswith("/api/settings/subscription-status"):
            assert not has_gate, f"billing must stay reachable to renew: {r.path}"
    assert not missing, f"product routes not behind the subscription gate: {missing}"
    assert ungated_ok  # documents the intentionally open prefixes


def test_unpriced_model_is_billed_at_provider_fallback_not_free():
    assert pricing.estimate_cost_usd("gemini", "some-new-model", 1000, 1000) is None
    fb = pricing.fallback_cost_usd("gemini", 1_000_000, 1_000_000)
    assert fb is not None and fb >= Decimal("2.50")  # dearest gemini output price
    assert pricing.fallback_cost_usd("gemini", None, 5) is None
