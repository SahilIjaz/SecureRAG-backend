from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WalletTransaction(Base):
    """
    Append-only ledger for the prepaid wallet — every top-up and every
    deduction gets a row here, `balance_after` snapshotting the running
    total so this reads back as a real statement, not something that has
    to be reconstructed from LLMUsageLog after the fact. See
    app/services/wallet_service.py for the only code that writes these.
    """

    __tablename__ = "wallet_transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # "topup" | "deduction" | "refund"
    amount_usd: Mapped[Numeric] = mapped_column(Numeric(10, 4), nullable=False)  # signed: + topup, - deduction
    balance_after: Mapped[Optional[Numeric]] = mapped_column(Numeric(10, 4), nullable=True)
    related_usage_log_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("llm_usage_logs.id", ondelete="SET NULL"), nullable=True
    )
    stripe_payment_intent_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    def __repr__(self) -> str:
        return f"<WalletTransaction id={self.id} tenant_id={self.tenant_id} type={self.type} amount={self.amount_usd}>"
