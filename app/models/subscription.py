from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, Enum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.tenant import Tenant
    from app.models.tenant_quota import TenantQuota

class PlanName(str, enum.Enum):
    free = "free"
    pro = "pro"
    pro_plus = "pro_plus"

class BillingCycle(str, enum.Enum):
    monthly = "monthly"
    yearly = "yearly"

class SubscriptionStatus(str, enum.Enum):
    active = "active"
    expired = "expired"
    cancelled = "cancelled"
    trial = "trial"

class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    plan_name: Mapped[PlanName] = mapped_column(
        Enum(PlanName, name="planname", create_type=True),
        nullable=False,
        default=PlanName.free,
    )
    billing_cycle: Mapped[Optional[BillingCycle]] = mapped_column(
        Enum(BillingCycle, name="billingcycle", create_type=True),
        nullable=True,
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, name="subscriptionstatus", create_type=True),
        nullable=False,
        default=SubscriptionStatus.active,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # Set the first time this tenant starts a 3-day trial; once set, they never
    # get another free trial — a re-checkout charges the card immediately
    # (see stripe_service.start_trial_subscription).
    trial_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Stripe linkage — populated once a real subscription exists (see
    # app/services/stripe_service.py). stripe_price_id tracks exactly which
    # Price the subscription is on, independent of plan_name's coarser
    # free/pro/pro_plus grouping.
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, unique=True, index=True)
    stripe_price_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    tenant: Mapped["Tenant"] = relationship(
        "Tenant",
        back_populates="subscription",
    )
    tenant_quota: Mapped[Optional["TenantQuota"]] = relationship(
        "TenantQuota",
        back_populates="subscription",
        uselist=False,
    )

    def __repr__(self) -> str:
        return f"<Subscription id={self.id} tenant_id={self.tenant_id} plan={self.plan_name}>"
