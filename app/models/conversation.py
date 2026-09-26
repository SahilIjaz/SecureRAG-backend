from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
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
    # Soft delete: set when an owner deletes a conversation from the inbox.
    # Excluded from all normal list/get queries; hard-purged by
    # conversation_service.trash_purge_loop() after
    # settings.CONVERSATION_TRASH_RETENTION_DAYS, giving a short recovery
    # window for an accidental delete.
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # True only once an owner has actively "joined" a real-time exchange with
    # the visitor (see the live-agent-handoff plan). Deliberately NOT what
    # gates whether the bot answers — that's status == "Handed off" (set the
    # moment escalation starts, well before an owner may have joined). This
    # flag only controls the live-polling UI once that plan's later steps
    # wire it up; it exists now so the column is in place ahead of that.
    is_live: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Which team member (tenant_users) is handling this live conversation, once
    # RBAC introduces multiple agents. Null = unassigned. Set when an agent
    # joins (POST /api/conversations/{id}/join).
    assigned_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Who resolved the conversation — assignment credit that must survive a
    # later transfer, so it's stored separately from assigned_user_id.
    resolved_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Set/reset every time a visitor escalates (POST /api/public/widget/escalate)
    # or the automatic low-confidence handoff fires. Used to compute the
    # connecting/unavailable state within LIVE_JOIN_TIMEOUT_SECONDS — see
    # app/api/public/widget.py:_compute_live_state.
    live_wait_started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Bumped on every widget request tied to this conversation while
    # escalated (escalate call, a message, or a live-status poll — the poll
    # is the main signal, since it's what runs continuously while the tab
    # is open and waiting). Lets join_conversation() refuse to go "live" if
    # the visitor no longer appears to actually have the chat open — see
    # helpers.is_visitor_still_present.
    visitor_last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
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
    # Python-side default (not just server_default): Postgres now() returns the
    # TRANSACTION start time, so a user message and its bot reply written in one
    # transaction would share a timestamp and make response time compute to 0.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
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
