from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, DateTime, String, func
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
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
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

    # Relationships
    user: Mapped[Optional["User"]] = relationship(
        "User",
        back_populates="tenant",
        uselist=False,
    )
    subscription: Mapped[Optional["Subscription"]] = relationship(
        "Subscription",
        back_populates="tenant",
        uselist=False,
    )
    tenant_quota: Mapped[Optional["TenantQuota"]] = relationship(
        "TenantQuota",
        back_populates="tenant",
        uselist=False,
    )
    usage_counts: Mapped[List["UsageCount"]] = relationship(
        "UsageCount",
        back_populates="tenant",
    )
    documents: Mapped[List["Document"]] = relationship(
        "Document",
        back_populates="tenant",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Tenant id={self.id} slug={self.slug!r}>"
