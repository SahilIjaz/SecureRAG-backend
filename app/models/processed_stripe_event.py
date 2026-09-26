from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

class ProcessedStripeEvent(Base):
    """
    Stripe webhook event IDs we've already handled, for idempotent delivery.

    Stripe may deliver the same event more than once (retries, at-least-once
    semantics). Without dedup a duplicate could, e.g., double-apply a plan
    change. The webhook inserts the event id here inside the same transaction
    that processes the event; a duplicate hits the unique primary key and is
    skipped.
    """
    __tablename__ = "processed_stripe_events"

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<ProcessedStripeEvent {self.event_id}>"
