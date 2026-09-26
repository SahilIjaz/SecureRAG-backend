from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.tenant_user import TenantRole, tenant_role_type

class Invite(Base):
    """
    A pending invitation for someone to join a tenant as an agent.

    The emailed link carries a one-time token; only its bcrypt hash is stored
    here (never the raw token), the same way OTPs are stored. Accepting the
    link IS the email verification — the invitee proved control of the mailbox
    by clicking it — so no separate OTP is issued (see accept-invite).
    """
    __tablename__ = "invites"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    role: Mapped[TenantRole] = mapped_column(
        tenant_role_type,
        nullable=False,
        default=TenantRole.agent,
    )
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    @property
    def is_pending(self) -> bool:
        return self.accepted_at is None and self.revoked_at is None

    def __repr__(self) -> str:
        return f"<Invite {self.email} tenant={self.tenant_id} pending={self.is_pending}>"
