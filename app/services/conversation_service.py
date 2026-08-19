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
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

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
