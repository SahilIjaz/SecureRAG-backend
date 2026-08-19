from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.tenant import Tenant

# Status / sentiment / channel are stored as plain strings in the exact
# casing the dashboard uses ("Open" / "Handed off" / "Resolved", etc.) so
# list endpoints are a straight pass-through.
CONVERSATION_STATUSES = ("Open", "Handed off", "Resolved")
CONVERSATION_SENTIMENTS = ("Positive", "Neutral", "Negative")
CONVERSATION_CHANNELS = ("Widget", "API", "Internal Test")

class Conversation(Base):
    """A visitor chat session with the tenant's chatbot."""

    __tablename__ = "conversations"

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
    visitor_name: Mapped[str] = mapped_column(String(255), nullable=False, default="Visitor")
    visitor_email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    visitor_phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="Open", index=True)
    sentiment: Mapped[str] = mapped_column(String(20), nullable=False, default="Neutral")
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="Widget")
    topic: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    unresolved_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    tenant: Mapped["Tenant"] = relationship("Tenant")
    messages: Mapped[List["ConversationMessage"]] = relationship(
        "ConversationMessage",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ConversationMessage.created_at",
    )

    def __repr__(self) -> str:
        return f"<Conversation id={self.id} tenant_id={self.tenant_id} status={self.status!r}>"

class ConversationMessage(Base):
    """One message inside a conversation. role: user | bot | agent."""

    __tablename__ = "conversation_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        index=True,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(10), nullable=False, default="user")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # Citation data for a bot reply — [{documentId, documentName, snippet, page, score}, ...].
    # Only ever populated for role="bot"; null for user/agent messages.
    sources: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    conversation: Mapped["Conversation"] = relationship(
        "Conversation",
        back_populates="messages",
    )

    def __repr__(self) -> str:
        return f"<ConversationMessage id={self.id} role={self.role!r}>"
