from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, SmallInteger, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AuditLog(Base):
    """
    One immutable row per security-relevant action: who did what, to which
    resource, when, from where, and whether it succeeded.

    Written by app/services/audit_service.py only — from the HTTP audit
    middleware (every mutating dashboard/API request) and from a few
    explicit call sites that know more than the middleware can (login
    attempts by email, password changes, exports).

    Tamper evidence: rows form a per-tenant hash chain. `entry_hash` is the
    SHA-256 of the row's canonical content plus the previous row's hash for
    the same tenant; `GET /api/audit/verify` recomputes the chain. The table
    also carries a database trigger that rejects UPDATE/DELETE except from
    the retention sweep (see migrations/add_audit_trail.sql), which protects
    against accidental or application-level edits and ORM deletes. A
    compromised database credential is out of scope (it could drop the
    trigger) — pair with backups/WAL for that.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_logs_tenant_action", "tenant_id", "action"),
        Index("ix_audit_logs_tenant_seq", "tenant_id", "seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Null only for events with no tenant context yet (a failed login for an
    # unknown email); those chain under the "global" key.
    tenant_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Deliberately NOT a foreign key: SET NULL on user deletion would be an
    # UPDATE on an append-only table (rejected by the trigger) and would
    # change hashed content. The id stays as recorded.
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    actor_role: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # Dotted verb, e.g. "document.delete", "auth.login", "settings.workspace.update".
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    resource_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    resource_label: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    outcome: Mapped[str] = mapped_column(String(8), nullable=False)  # success | failure
    status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    method: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    request_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    details: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # Per-tenant monotonic sequence assigned under the append lock — the
    # chain order, independent of wall-clock skew between app instances.
    seq: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    prev_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Which canonicalisation scheme produced entry_hash (audit_service.HASH_VERSION).
    # Rows that predate the column verify as version 1.
    hash_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=2, server_default="1")
    # Set by the application (not server_default) because it is part of the
    # hashed content.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} tenant={self.tenant_id} outcome={self.outcome}>"
