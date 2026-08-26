from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.subscription import Subscription
    from app.models.tenant_quota import TenantQuota
    from app.models.usage_count import UsageCount
    from app.models.document import Document

class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        index=True,
    )
    workspace_name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    business_category: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    employee_count_range: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    has_documents: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    onboarding_completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Presence (live-agent-handoff). is_owner_online is a cache updated by
    # POST /api/presence/ping; last_seen_at is the source of truth — any
    # reader should compute actual online-ness as
    # is_owner_online AND (now() - last_seen_at) < PRESENCE_ONLINE_WINDOW_SECONDS
    # (see app/api/frontend/helpers.py:is_owner_currently_online), since
    # nothing flips the boolean back to False on a dropped connection.
    is_owner_online: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Prepaid wallet + one-time signup trial (see app/services/wallet_service.py) ──
    balance_usd: Mapped[Numeric] = mapped_column(Numeric(10, 4), default=0, nullable=False)
    # Both caps checked together — whichever is hit first ends the trial,
    # permanently (trial_ended_at, once set, is never cleared by any code
    # path). trial_messages_used only counts real answer calls, never the
    # background sentiment/topic classifier — see wallet_service.record_usage.
    trial_messages_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    trial_ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Mirrors UsageCount.warned_80_percent's "fire once, not on every call
    # past the threshold" shape — reset to False on a top-up that brings the
    # balance back above the alert threshold (see stripe_service).
    wallet_low_balance_warned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Tenant's chosen LLM provider/model for their chatbot — null means "use
    # the platform default chain" (today's behavior). Kept directly on
    # Tenant (not a separate settings table) since it's read on every widget
    # chat request, the same hot path balance_usd/trial fields are on.
    preferred_llm_provider: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    preferred_llm_model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # passive_deletes=True on every relationship below: all of these FKs
    # already have ondelete="CASCADE" at the DB level (see each model's
    # ForeignKey("tenants.id", ondelete="CASCADE")). Without passive_deletes,
    # SQLAlchemy's default behavior on session.delete(tenant) is to try to
    # manage these relationships itself — for the ones with no delete
    # cascade (user/subscription/tenant_quota/usage_counts), that means
    # issuing "UPDATE ... SET tenant_id = NULL" for any loaded child, which
    # crashes since tenant_id is NOT NULL everywhere. passive_deletes=True
    # tells the ORM to leave deletion of children entirely to the database's
    # own ON DELETE CASCADE instead.
    user: Mapped[Optional["User"]] = relationship(
        "User",
        back_populates="tenant",
        uselist=False,
        passive_deletes=True,
    )
    subscription: Mapped[Optional["Subscription"]] = relationship(
        "Subscription",
        back_populates="tenant",
        uselist=False,
        passive_deletes=True,
    )
    tenant_quota: Mapped[Optional["TenantQuota"]] = relationship(
        "TenantQuota",
        back_populates="tenant",
        uselist=False,
        passive_deletes=True,
    )
    usage_counts: Mapped[List["UsageCount"]] = relationship(
        "UsageCount",
        back_populates="tenant",
        passive_deletes=True,
    )
    documents: Mapped[List["Document"]] = relationship(
        "Document",
        back_populates="tenant",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<Tenant id={self.id} slug={self.slug!r}>"
