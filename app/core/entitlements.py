"""
Single source of truth for what each plan is entitled to — every numeric limit
AND every boolean feature flag.

Before this module the per-plan numbers lived in auth_service.PLAN_QUOTAS (three
values only) and a parallel display-only dict in api/frontend/helpers.py, while
"features" were never gated at all. Route every plan decision through here so
there is exactly one place that answers "what can this plan do".

The keys are the internal PlanName enum (free/pro/pro_plus). Those map to the
user-facing Starter/Growth/Business via FE_TO_BE_PLAN in helpers.py — this module
never deals in the user-facing names.

A value of -1 on any numeric limit means "unlimited" (matches the existing
TenantQuota convention).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.models.subscription import PlanName, Subscription


@dataclass(frozen=True)
class Entitlements:
    # Numeric limits
    max_documents: int
    max_file_size_mb: int
    max_questions_per_month: int
    max_urls: int
    max_faqs: int
    max_chunks: int
    max_storage_mb: int
    # Feature flags
    live_handoff: bool
    remove_branding: bool
    lead_capture: bool
    menu: bool

    def quota_dict(self) -> dict:
        """The three fields TenantQuota persists, for writing a quota row."""
        return {
            "max_documents": self.max_documents,
            "max_file_size_mb": self.max_file_size_mb,
            "max_questions_per_month": self.max_questions_per_month,
        }


# Costed against the Stripe pricing model: Starter $10, Growth $22, Business $105.
PLAN_ENTITLEMENTS: dict[PlanName, Entitlements] = {
    PlanName.free: Entitlements(  # "Starter"
        max_documents=25,
        max_file_size_mb=20,
        max_questions_per_month=500,
        max_urls=25,
        max_faqs=50,
        max_chunks=2000,
        max_storage_mb=200,
        live_handoff=False,
        remove_branding=False,
        lead_capture=False,
        menu=False,
    ),
    PlanName.pro: Entitlements(  # "Growth"
        max_documents=150,
        max_file_size_mb=50,
        max_questions_per_month=5000,
        max_urls=150,
        max_faqs=300,
        max_chunks=12000,
        max_storage_mb=1024,
        live_handoff=True,
        remove_branding=False,
        lead_capture=True,
        menu=True,
    ),
    PlanName.pro_plus: Entitlements(  # "Business"
        max_documents=1000,
        max_file_size_mb=100,
        max_questions_per_month=12000,
        max_urls=1000,
        max_faqs=2000,
        max_chunks=80000,
        max_storage_mb=5120,
        live_handoff=True,
        remove_branding=True,
        lead_capture=True,
        menu=True,
    ),
}

# The plan an entitlement lookup falls back to when a subscription is missing or
# carries an unrecognized plan — the most restrictive tier, so a bug can never
# accidentally grant more than the cheapest plan.
_DEFAULT_PLAN = PlanName.free


def entitlements_for_plan(plan: Optional[PlanName]) -> Entitlements:
    if plan is None:
        return PLAN_ENTITLEMENTS[_DEFAULT_PLAN]
    return PLAN_ENTITLEMENTS.get(plan, PLAN_ENTITLEMENTS[_DEFAULT_PLAN])


def get_entitlements(subscription: Optional[Subscription]) -> Entitlements:
    """Resolve entitlements from the *server-side* subscription row — never from
    anything the client sent. This is the function every route should use."""
    plan = subscription.plan_name if subscription is not None else None
    return entitlements_for_plan(plan)
