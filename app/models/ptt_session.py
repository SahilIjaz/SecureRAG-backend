from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PTTStatus(str, enum.Enum):
    initiated = "initiated"   # request sent, not yet accepted
    active = "active"         # accepted; live audio channel open
    ended = "ended"          # finished normally
    failed = "failed"        # rejected, or the target never connected


# One shared Enum instance for the Postgres type, so metadata.create_all emits
# CREATE TYPE exactly once regardless of build order.
ptt_status_type = Enum(PTTStatus, name="pttstatus", create_type=True)


class PTTSession(Base):
    """
    One push-to-talk voice session between two people in a conversation.

    A PTT session is scoped to a live conversation (the "room"): the visitor on
    the widget and the agent who joined it on the dashboard. The server only
    relays WebRTC signaling over the websocket — it never touches the audio,
    which flows peer-to-peer. This row is the durable record for history and
    reporting; the live session state also lives in memory in the websocket
    connection manager while the call is up.
    """
    __tablename__ = "ptt_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The public session id used in every websocket signaling event.
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Participants. The initiator/target is either the assigned agent (a user)
    # or the widget visitor; we store the party kind rather than a hard FK to
    # both tables. For an agent this is the user id; for a visitor it's the
    # conversation's visitor identity.
    initiator_id: Mapped[str] = mapped_column(String(128), nullable=False)
    initiator_kind: Mapped[str] = mapped_column(String(16), nullable=False)  # agent | visitor
    target_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    target_kind: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    status: Mapped[PTTStatus] = mapped_column(
        ptt_status_type, nullable=False, default=PTTStatus.initiated, index=True
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Whole seconds the audio channel was active (start → end), computed server-side.
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    # Call transcript — produced after the call from the agent-side recording
    # (see app/services/call_transcript_service.py). Plain text, one line per
    # utterance: "<Speaker name>: <what they said>". transcript_status tracks
    # the async pipeline: pending → processing → done | failed | skipped.
    transcript: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    transcript_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    def __repr__(self) -> str:
        return f"<PTTSession {self.session_id} conv={self.conversation_id} status={self.status}>"
