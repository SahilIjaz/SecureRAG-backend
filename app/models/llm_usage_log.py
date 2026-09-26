from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LLMUsageLog(Base):
    """
    Per-call record of what was actually spent — one row per LLM call
    (answer or classification), whether or not it was covered by the
    signup trial. `raw_cost_usd` is the real $ owed to the provider, no
    markup (that's applied only at the WalletTransaction deduction step);
    null means "cost unknown" (e.g. a stream that died with no terminal
    usage chunk, or a call still inside the tenant's trial), never a
    silent 0 — see app/services/wallet_service.py.
    """

    __tablename__ = "llm_usage_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    call_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "answer" | "classification"
    prompt_tokens: Mapped[Optional[int]] = mapped_column(nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(nullable=True)
    total_tokens: Mapped[Optional[int]] = mapped_column(nullable=True)
    raw_cost_usd: Mapped[Optional[Numeric]] = mapped_column(Numeric(10, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    def __repr__(self) -> str:
        return f"<LLMUsageLog id={self.id} tenant_id={self.tenant_id} provider={self.provider} model={self.model}>"
