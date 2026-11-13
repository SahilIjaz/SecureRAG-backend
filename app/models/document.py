from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.tenant import Tenant

class DocumentSource(str, enum.Enum):
    uploaded = "uploaded"
    sample = "sample"
    scraped = "scraped"
    faq = "faq"

class DocumentStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    ready = "ready"
    failed = "failed"

class ApprovalStatus(str, enum.Enum):
    """Administrative verification state of a document's *content*.

    Stored as a plain VARCHAR (not a Postgres enum) so adding a state never
    needs an ALTER TYPE. Only workspace admins/owners can change it — see
    app/services/document_authority_service.py — and nothing in ingestion or
    generation ever writes it, so document text or an LLM reply can never
    grant a document authority.
    """
    unverified = "unverified"   # default for every new upload/scrape/FAQ
    approved = "approved"       # an admin confirmed this is the current truth
    deprecated = "deprecated"   # an admin marked it outdated (kept for history)

class Document(Base):
    """
    Stores every document associated with a tenant — both real uploads
    and sample documents the user selected during onboarding.
    """
    __tablename__ = "documents"
    __table_args__ = (
        # (id, tenant_id) is unique by virtue of id being the PK; declaring
        # it lets the composite self-FK below reference it, which is what
        # makes "a document may only supersede a document of its own tenant"
        # a database guarantee rather than an application convention.
        UniqueConstraint("id", "tenant_id", name="ux_documents_id_tenant"),
        ForeignKeyConstraint(
            ["supersedes_document_id", "tenant_id"],
            ["documents.id", "documents.tenant_id"],
            name="fk_documents_supersedes_same_tenant",
        ),
        CheckConstraint(
            "supersedes_document_id IS DISTINCT FROM id",
            name="ck_documents_not_self_superseding",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_path: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    file_url: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    file_size_mb: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False, default="application/pdf")
    source: Mapped[DocumentSource] = mapped_column(
        Enum(DocumentSource, name="documentsource", create_type=False),
        nullable=False,
        default=DocumentSource.uploaded,
        index=True,
    )
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="documentstatus", create_type=False),
        nullable=False,
        default=DocumentStatus.pending,
        index=True,
    )
    sample_document_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sample_documents.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_url: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    # Only populated for source == faq — the manually-entered Q&A text
    # itself, since a FAQ document has no file to download/extract from.
    question: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Last indexing/scrape failure reason (truncated), surfaced in the
    # Knowledge page so "Failed" isn't a dead end. Cleared when re-queued.
    error_message: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # SHA-256 hex digest of the raw file bytes — lets upload_documents() detect
    # a byte-identical re-upload before it burns a Cloudinary + Pinecone cost.
    # Only populated for source == uploaded (see document_service.upload_documents).
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ── Authority metadata (admin-maintained; see document_authority_service) ──
    # Used by conflict *resolution* only: when two retrieved documents
    # contradict each other, these fields are the sole evidence the resolver
    # may use to prefer one. Defaults make every existing row "unverified,
    # v1, no effective date, supersedes nothing, rank 0" — i.e. nothing is
    # resolved automatically until an admin says so.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    effective_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    approval_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ApprovalStatus.unverified.value,
        server_default=ApprovalStatus.unverified.value,
    )
    # Explicit "this document replaces that one" link, same tenant only
    # (enforced by fk_documents_supersedes_same_tenant). Not an FK on the
    # single column on purpose — the composite FK carries the tenant check.
    supersedes_document_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Admin-assigned source priority (0 = normal). Higher wins ties between
    # two otherwise equally applicable documents. Never derived from scores.
    authority_rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Audit trail for the fields above.
    authority_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    authority_updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="documents")

    def __repr__(self) -> str:
        return f"<Document id={self.id} name={self.original_filename!r} source={self.source}>"
