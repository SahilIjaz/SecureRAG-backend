from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.tenant import Tenant

class UsageCount(Base):
    """
    Tracks monthly resource consumption per tenant.
    period_month always stores the first day of the relevant month (e.g. 2026-03-01).
    """

    __tablename__ = "usage_counts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "period_month", name="uq_usage_tenant_period"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    period_month: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    questions_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    documents_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    storage_used_mb: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Set once each threshold is crossed this period_month, so the plan-usage
    # warning fires exactly once per threshold instead of on every message
    # past it — see notification_service's usage-warning check.
    warned_80_percent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    warned_100_percent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    tenant: Mapped["Tenant"] = relationship(
        "Tenant",
        back_populates="usage_counts",
    )

    def __repr__(self) -> str:
        return (
            f"<UsageCount id={self.id} tenant_id={self.tenant_id} "
            f"period={self.period_month} questions={self.questions_used}>"
        )
