from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.tenant import Tenant

class DocumentSource(str, enum.Enum):
    uploaded = "uploaded"
    sample = "sample"
    scraped = "scraped"

class DocumentStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    ready = "ready"
    failed = "failed"

class Document(Base):
    """
    Stores every document associated with a tenant — both real uploads
    and sample documents the user selected during onboarding.
    """
    __tablename__ = "documents"

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
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="documents")

    def __repr__(self) -> str:
        return f"<Document id={self.id} name={self.original_filename!r} source={self.source}>"
