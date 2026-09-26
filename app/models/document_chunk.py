from __future__ import annotations

import uuid

from sqlalchemy import Computed, ForeignKey, Index, Integer, Text
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

class DocumentChunk(Base):
    """
    Postgres copy of each indexed chunk's text, used for the keyword half of
    hybrid retrieval.

    Before this, chunk text lived only in Pinecone vector metadata, so keyword
    (lexical) search was impossible — retrieval was pure dense vectors and
    missed exact terms (product codes, names, error strings) that embeddings
    blur. This table mirrors every chunk at index time; `ts` is a Postgres
    generated tsvector with a GIN index, so `to_tsquery` full-text search is a
    fast index scan. Tenant isolation is by the tenant_id filter, matching the
    Pinecone namespace.
    """
    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The Pinecone chunk_id, so a keyword hit can be de-duplicated against a
    # vector hit for the same chunk during fusion.
    chunk_id: Mapped[str] = mapped_column(Text, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # Generated full-text column — Postgres keeps it in sync with `text`.
    ts: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', text)", persisted=True),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_document_chunks_ts", "ts", postgresql_using="gin"),
        Index("ix_document_chunks_tenant_doc", "tenant_id", "document_id"),
    )

    def __repr__(self) -> str:
        return f"<DocumentChunk doc={self.document_id} seq={self.sequence}>"
