from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AnswerTrace(Base):
    """
    Response traceability: one row per generated reply recording exactly
    what the pipeline saw and decided — the retrieved chunks (with their
    retrieval and rerank scores), the computed and self-reported confidence,
    the gate decision, any contradictions and how they were resolved, and
    the model/tokens used. Written for real widget visitors, the public bot
    page and dashboard test chats alike, and linked from the stored bot
    message (`conversation_messages.trace_id`) so an admin can open
    "Why this answer?" months later.

    Never contains the prompt itself (the system prompt is not tenant data)
    and snippets are capped (TRACE_SNIPPET_CHARS) — the full chunk text is
    recoverable from document_chunks by document_id + chunk_id. It DOES
    store the visitor's question and the reply verbatim: traces are deleted
    with their conversation, with the message purge, and after
    TRACE_RETENTION_DAYS.
    """

    __tablename__ = "answer_traces"
    __table_args__ = (
        Index("ix_answer_traces_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # CASCADE: deleting a conversation (owner delete, trash purge) removes
    # its traces too — a visitor's question/answer text never outlives the
    # conversation it belongs to.
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    message_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)  # widget | public | test
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    # answer | clarify | handoff | refuse | conflict_blocked | no_context | empty
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    confidence: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    provider: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retrieval: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)  # similarity/coverage/entropy/…
    chunks: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)     # retrieved evidence
    conflicts: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)  # detected + resolution
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    def __repr__(self) -> str:
        return f"<AnswerTrace {self.id} tenant={self.tenant_id} decision={self.decision}>"
