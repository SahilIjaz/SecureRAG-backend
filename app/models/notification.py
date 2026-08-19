from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.tenant import Tenant

# Matches NotificationSetting's boolean columns 1:1 — notification_service
# uses this string directly as getattr(setting, type) to decide whether an
# email should also go out for a given in-app notification.
NOTIFICATION_TYPES = (
    "unresolved_conversations",
    "weekly_summary",
    "knowledge_gaps_detected",
    "plan_usage_warnings",
    "product_updates",
)

class Notification(Base):
    """
    In-app notification feed (the dashboard bell). Always written regardless
    of the tenant's NotificationSetting toggles — those toggles gate email
    delivery only, not this feed; see notification_service.notify_tenant().
    """

    __tablename__ = "notifications"

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
    type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    link: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    tenant: Mapped["Tenant"] = relationship("Tenant")

    def __repr__(self) -> str:
        return f"<Notification id={self.id} type={self.type!r} is_read={self.is_read}>"
