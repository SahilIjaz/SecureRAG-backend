from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

class TenantRole(str, enum.Enum):
    owner = "owner"    # full control incl. billing, workspace delete, members
    admin = "admin"    # manage knowledge base, chatbot config, conversations
    agent = "agent"    # handle conversations only

# One shared Enum instance for the Postgres `tenantrole` type — imported by both
# tenant_users and invites so metadata.create_all emits CREATE TYPE exactly once
# (checkfirst) no matter which table it builds first.
tenant_role_type = Enum(TenantRole, name="tenantrole", create_type=True)

class TenantUser(Base):
    """
    Membership of a user in a tenant, with a role.

    The product is one-user-per-tenant today (User.tenant_id is UNIQUE), so this
    table starts as a 1:1 mirror where that single user is the `owner`. It exists
    so role checks have a home NOW (see core/rbac.py) and so multi-user support
    later is an additive change — add rows here and relax the User.tenant_id
    unique constraint — rather than a rewrite of every authorization check.
    """
    __tablename__ = "tenant_users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_tenant_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[TenantRole] = mapped_column(
        tenant_role_type,
        nullable=False,
        default=TenantRole.owner,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<TenantUser tenant={self.tenant_id} user={self.user_id} role={self.role}>"
