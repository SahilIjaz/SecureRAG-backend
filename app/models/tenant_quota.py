from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.tenant import Tenant
    from app.models.subscription import Subscription


class TenantQuota(Base):
    """
    Stores per-tenant resource limits derived from their active subscription plan.
    A value of -1 means unlimited for any quota field.
    """

    __tablename__ = "tenant_quotas"

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
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    max_documents: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    max_file_size_mb: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    max_questions_per_month: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
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

    tenant: Mapped["Tenant"] = relationship(
        "Tenant",
        back_populates="tenant_quota",
    )
    subscription: Mapped["Subscription"] = relationship(
        "Subscription",
        back_populates="tenant_quota",
    )

    def __repr__(self) -> str:
        return (
            f"<TenantQuota id={self.id} tenant_id={self.tenant_id} "
            f"max_docs={self.max_documents} max_q={self.max_questions_per_month}>"
        )

