"""
Prepaid wallet + one-time signup trial.

There is no permanent free tier. A new tenant gets a small, one-time,
non-renewing trial on the platform default provider only (message count AND
day window, whichever hits first); once it ends — or for any non-default
provider from the start — every call is metered through the wallet at
raw cost * WALLET_MARKUP_MULTIPLIER. See NexusContext plan doc
i-want-to-implement-floofy-hickey.md section C for the full design.

Two entry points, with two different correctness rules — do not merge them:
  - check_can_generate(): call BEFORE invoking the LLM. Allowed to refuse.
  - record_usage(): call AFTER a call succeeds (or fails with partial
    output). Never refuses — the cost was already, really, incurred, so
    this always records it, even if it pushes balance_usd negative.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.llm_usage_log import LLMUsageLog
from app.models.tenant import Tenant
from app.models.wallet_transaction import WalletTransaction
from app.services.llm import pricing
from app.services.llm.base import GenerationUsage
from app.services.notification_service import notify_tenant

logger = logging.getLogger(__name__)

# The trial only ever applies to this provider — never to whatever a tenant
# explicitly picks (Claude, or any future paid provider). Letting the trial
# cover a paid provider would mean Nexus eating real cost for an unproven
# signup, which is precisely what this whole design exists to prevent.
DEFAULT_PROVIDER = "gemini"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class GenerationGate:
    allowed: bool
    block_reason: Optional[str] = None


def _still_in_trial(tenant: Tenant) -> bool:
    if tenant.trial_ended_at is not None:
        return False
    within_days = _utcnow() - _as_aware(tenant.created_at) < timedelta(days=settings.TRIAL_DURATION_DAYS)
    within_messages = (tenant.trial_messages_used or 0) < settings.TRIAL_MESSAGE_LIMIT
    return within_days and within_messages


async def check_can_generate(tenant: Tenant, provider: str, db: AsyncSession) -> GenerationGate:
    """
    Pre-generation gate. Two paths to "allowed":
      - Still within the trial AND this call is on the default provider.
      - Wallet balance > 0 (any provider, including the default once its
        trial has ended).

    Mutates `tenant.trial_ended_at` in place (and notifies once) the moment
    the trial is first found to be over — the caller's own flush/commit
    persists that; this function never commits.
    """
    if provider == DEFAULT_PROVIDER and tenant.trial_ended_at is None:
        if _still_in_trial(tenant):
            return GenerationGate(allowed=True)
        tenant.trial_ended_at = _utcnow()
        await notify_tenant(
            tenant.id, "plan_usage_warnings",
            "Your free trial has ended",
            "Add funds to your wallet to keep your chatbot answering questions.",
            "/dashboard/settings?tab=billing", db,
        )

    if tenant.balance_usd is not None and tenant.balance_usd > 0:
        return GenerationGate(allowed=True)
    return GenerationGate(allowed=False, block_reason=f"top up your balance to keep using {provider}")


async def record_usage(
    *,
    tenant_id: uuid.UUID,
    conversation_id: Optional[uuid.UUID],
    call_type: str,
    usage: Optional[GenerationUsage],
    db: AsyncSession,
) -> None:
    """
    Post-call bookkeeping. Always safe to call, even for a failed/partial
    generation (usage=None just logs "cost unknown", no deduction attempted
    — never guess a deduction amount). Callers should wrap this in their own
    try/except so a bug here can never take down the real chat-message write
    it's bundled with (see widget.py).
    """
    if usage is None:
        db.add(LLMUsageLog(
            tenant_id=tenant_id, conversation_id=conversation_id,
            provider="unknown", model="unknown", call_type=call_type,
            prompt_tokens=None, completion_tokens=None, total_tokens=None, raw_cost_usd=None,
        ))
        return

    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        return

    # Free-riding during an active trial: a call on the default provider
    # while the trial hasn't ended yet costs nothing. Only a real "answer"
    # call counts against the tenant-visible trial message cap — the
    # background sentiment/topic classifier rides along uncounted, since a
    # tenant sending one message shouldn't see their trial deplete by two.
    free_this_call = usage.provider == DEFAULT_PROVIDER and tenant.trial_ended_at is None
    usage_log = LLMUsageLog(
        tenant_id=tenant_id, conversation_id=conversation_id,
        provider=usage.provider, model=usage.model, call_type=call_type,
        prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        raw_cost_usd=None if free_this_call else pricing.estimate_cost_usd(
            usage.provider, usage.model, usage.prompt_tokens, usage.completion_tokens
        ),
    )
    db.add(usage_log)

    if free_this_call:
        if call_type == "answer":
            tenant.trial_messages_used = (tenant.trial_messages_used or 0) + 1
        return

    if usage_log.raw_cost_usd is None:
        return  # cost unknown — never guess a deduction amount

    await db.flush()  # need usage_log.id for the ledger row below

    deduction = usage_log.raw_cost_usd * Decimal(str(settings.WALLET_MARKUP_MULTIPLIER))
    result = await db.execute(
        update(Tenant).where(Tenant.id == tenant_id)
        .values(balance_usd=Tenant.balance_usd - deduction)
        .returning(Tenant.balance_usd)
    )
    new_balance = result.scalar_one_or_none()
    db.add(WalletTransaction(
        tenant_id=tenant_id, type="deduction", amount_usd=-deduction,
        balance_after=new_balance, related_usage_log_id=usage_log.id,
    ))

    threshold = Decimal(str(settings.WALLET_LOW_BALANCE_ALERT_THRESHOLD_USD))
    if new_balance is not None and new_balance < threshold and not tenant.wallet_low_balance_warned:
        tenant.wallet_low_balance_warned = True
        await notify_tenant(
            tenant_id, "plan_usage_warnings",
            "Your wallet balance is running low",
            f"Add funds to keep your chatbot answering — balance is ${new_balance:.2f}.",
            "/dashboard/settings?tab=billing", db,
        )
