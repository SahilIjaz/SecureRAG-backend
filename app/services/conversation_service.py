"""
Conversation lifecycle housekeeping — the recurring counterpart to the
manual "Mark resolved" action in api/frontend/conversations.py.

Nothing marks a conversation Resolved except an agent doing it by hand or
this sweep — there's no other path, so an abandoned chat (visitor left,
question answered but nobody clicked resolve, or just a stray test message)
stays Open/Handed-off forever and the Conversations inbox only ever grows.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.conversation import Conversation, ConversationMessage

logger = logging.getLogger(__name__)

_SWEEP_INTERVAL_HOURS = 6
_OPEN_STATUSES = ("Open", "Handed off")

async def stale_conversation_sweep_loop() -> None:
    """Recurring background sweep, started once from the app lifespan — same
    fire-and-forget pattern as indexing_service.stale_document_sweep_loop()."""
    while True:
        try:
            await auto_resolve_stale_conversations()
        except Exception:
            logger.exception("Stale-conversation sweep iteration failed")
        await asyncio.sleep(_SWEEP_INTERVAL_HOURS * 3600)

async def auto_resolve_stale_conversations() -> int:
    """
    Resolve any Open/Handed-off conversation whose most recent message (or
    its own creation, if it never got a reply at all) is older than
    CONVERSATION_AUTO_RESOLVE_DAYS. Returns the number resolved.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.CONVERSATION_AUTO_RESOLVE_DAYS)

    async with AsyncSessionLocal() as db:
        last_activity = func.coalesce(func.max(ConversationMessage.created_at), Conversation.created_at)
        stale_ids_result = await db.execute(
            select(Conversation.id)
            .outerjoin(ConversationMessage, ConversationMessage.conversation_id == Conversation.id)
            .where(Conversation.status.in_(_OPEN_STATUSES))
            .group_by(Conversation.id)
            .having(last_activity < cutoff)
        )
        stale_ids = [row[0] for row in stale_ids_result.all()]
        if not stale_ids:
            return 0

        await db.execute(update(Conversation).where(Conversation.id.in_(stale_ids)).values(status="Resolved"))
        await db.commit()
        logger.info("Auto-resolved %d stale conversation(s) (no activity for %d+ days)",
                    len(stale_ids), settings.CONVERSATION_AUTO_RESOLVE_DAYS)
        return len(stale_ids)

_LIVE_FLAG_SWEEP_INTERVAL_SECONDS = 60

async def live_flag_sweep_loop() -> None:
    """Recurring background sweep, started once from the app lifespan — same
    fire-and-forget pattern as stale_conversation_sweep_loop() above, just on
    a much shorter interval to match VISITOR_PRESENCE_WINDOW_SECONDS's scale
    (minutes) instead of CONVERSATION_AUTO_RESOLVE_DAYS's (a week)."""
    while True:
        try:
            await clear_stale_live_flags()
        except Exception:
            logger.exception("Live-flag sweep iteration failed")
        await asyncio.sleep(_LIVE_FLAG_SWEEP_INTERVAL_SECONDS)

async def clear_stale_live_flags() -> int:
    """
    Drop is_live back to False once the visitor who triggered it has been
    gone longer than VISITOR_PRESENCE_WINDOW_SECONDS.

    is_live is otherwise only ever cleared by an explicit "Mark resolved" or
    by auto_resolve_stale_conversations() above — and that sweep only fires
    after CONVERSATION_AUTO_RESOLVE_DAYS (a week) of total silence. Without
    this, a conversation an owner joined keeps showing the dashboard's green
    "Live" badge for up to a week after the visitor actually left, even
    though real-time presence (visitor_last_seen_at) already knows better.

    Only the flag moves — status is untouched, so a "Handed off" chat just
    falls back to its normal amber "waiting" badge instead of green "Live"
    (see ConversationRow.tsx's isLive / status=="Handed off" branches).
    Returns the number cleared.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.VISITOR_PRESENCE_WINDOW_SECONDS)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(Conversation)
            .where(
                Conversation.is_live == True,
                or_(
                    Conversation.visitor_last_seen_at < cutoff,
                    Conversation.visitor_last_seen_at.is_(None),
                ),
            )
            .values(is_live=False)
        )
        await db.commit()
        if result.rowcount:
            logger.info("Cleared stale is_live flag on %d conversation(s)", result.rowcount)
        return result.rowcount

async def trash_purge_loop() -> None:
    """Recurring background sweep, started once from the app lifespan — same
    fire-and-forget pattern as stale_conversation_sweep_loop() above."""
    while True:
        try:
            await purge_expired_trash()
        except Exception:
            logger.exception("Trash-purge sweep iteration failed")
        await asyncio.sleep(_SWEEP_INTERVAL_HOURS * 3600)

async def purge_expired_trash() -> int:
    """
    Hard-delete (cascading to conversation_messages) any conversation whose
    deleted_at is older than CONVERSATION_TRASH_RETENTION_DAYS. This is the
    permanent-delete step behind the "soft delete" in
    api/frontend/conversations.py's DELETE endpoint — deleting gives a short
    recovery window instead of being instant and irreversible. Returns the
    number purged.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.CONVERSATION_TRASH_RETENTION_DAYS)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            delete(Conversation)
            .where(Conversation.deleted_at.is_not(None), Conversation.deleted_at < cutoff)
        )
        await db.commit()
        count = result.rowcount or 0
        if count:
            logger.info("Purged %d soft-deleted conversation(s) past the %d-day trash window",
                        count, settings.CONVERSATION_TRASH_RETENTION_DAYS)
        return count

async def purge_old_message_text(tenant_id: uuid.UUID, db: AsyncSession) -> int:
    """
    Manual, admin-triggered (see api/frontend/conversations.py's
    /purge-old-messages) — not a recurring sweep, per the explicit "manual
    button" decision in NexusContext/LIVE_AGENT_HANDOFF_PLAN.md gap #4.

    Deletes ConversationMessage rows — never the parent Conversation, whose
    summary columns (status/topic/sentiment/channel/created_at) are what
    Overview stats actually read, so they stay accurate with or without the
    transcript text still existing — for this tenant's Resolved
    conversations whose last activity is older than the retention window.
    Conversations with at least one agent-authored message get
    MESSAGE_RETENTION_DAYS_AGENT_INVOLVED (longer — more likely to matter
    for QA/disputes); bot-only ones get MESSAGE_RETENTION_DAYS_BOT_ONLY.
    Uses the same last-activity-from-messages computation as
    auto_resolve_stale_conversations() above, not Conversation.updated_at
    (which only bumps when the Conversation row itself changes, not when a
    child message is added — see that gap's analysis).
    """
    now = datetime.now(timezone.utc)
    last_activity = func.coalesce(func.max(ConversationMessage.created_at), Conversation.created_at)
    has_agent_message = func.bool_or(ConversationMessage.role == "agent")

    rows = await db.execute(
        select(Conversation.id, last_activity.label("last_activity"), has_agent_message.label("has_agent"))
        .outerjoin(ConversationMessage, ConversationMessage.conversation_id == Conversation.id)
        .where(Conversation.tenant_id == tenant_id, Conversation.status == "Resolved")
        .group_by(Conversation.id)
    )

    to_purge = []
    for convo_id, last_active, has_agent in rows.all():
        if last_active is None:
            continue
        retention_days = (
            settings.MESSAGE_RETENTION_DAYS_AGENT_INVOLVED
            if has_agent
            else settings.MESSAGE_RETENTION_DAYS_BOT_ONLY
        )
        if now - last_active > timedelta(days=retention_days):
            to_purge.append(convo_id)

    if not to_purge:
        return 0

    result = await db.execute(delete(ConversationMessage).where(ConversationMessage.conversation_id.in_(to_purge)))
    return result.rowcount or 0
