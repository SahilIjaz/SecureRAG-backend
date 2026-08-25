"""
Frontend-compat conversations endpoints (/api/conversations/...).

  - GET    /api/conversations              -> ConversationListItem[]
  - GET    /api/conversations/{id}         -> ConversationDetail (with messages)
  - POST   /api/conversations/{id}/reply   -> {message}  (human agent reply; marks it Handed off)
  - POST   /api/conversations/{id}/join    -> ConversationDetail  (owner joins live chat; sets is_live)
  - POST   /api/conversations/{id}/resolve -> ConversationDetail  (marks it Resolved; clears is_live)
  - DELETE /api/conversations/{id}         -> 204  (soft delete; recoverable for
                                               settings.CONVERSATION_TRASH_RETENTION_DAYS,
                                               see app/services/conversation_service.py)
  - POST   /api/conversations/purge-old-messages -> {purged: int}  (manual —
                                               see conversation_service.purge_old_message_text)
"""

import uuid
from datetime import datetime, timezone
from typing import List, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.frontend import helpers
from app.core.background import fire_and_forget
from app.database import get_db
from app.models.conversation import Conversation, ConversationMessage
from app.models.user import User
from app.schemas.frontend import (
    FEConversationDetail,
    FEConversationListItem,
    FEConversationMessage,
    FEPurgeMessagesResponse,
    FESendReplyRequest,
    FESendReplyResponse,
)
from app.services.auth_service import get_current_user
from app.services.conversation_service import purge_old_message_text
from app.services.notification_service import notify_visitor_of_reply

router = APIRouter(prefix="/conversations", tags=["Frontend — Conversations"])

def _preview(convo: Conversation) -> str:
    first_user_msg = next((m.text for m in convo.messages if m.role == "user"), "")
    return first_user_msg if len(first_user_msg) <= 60 else first_user_msg[:57] + "..."

def _list_item(convo: Conversation) -> FEConversationListItem:
    return FEConversationListItem(
        id=str(convo.id),
        name=convo.visitor_name,
        initials=helpers.initials(convo.visitor_name),
        preview=_preview(convo),
        timeAgo=helpers.time_ago(convo.created_at),
        createdAt=helpers._as_aware(convo.created_at).isoformat(),
        status=convo.status,
        sentiment=convo.sentiment,
        channel=convo.channel,
        isLive=convo.is_live,
        visitorEmail=convo.visitor_email,
        visitorPhone=convo.visitor_phone,
        visitorPresent=helpers.is_visitor_still_present(convo),
    )

def _message(msg: ConversationMessage) -> FEConversationMessage:
    return FEConversationMessage(
        id=str(msg.id),
        role=msg.role,
        text=msg.text,
        createdAt=helpers._as_aware(msg.created_at).isoformat(),
        sources=msg.sources or [],
    )

async def _get_conversation_or_404(
    conversation_id: str, user: User, db: AsyncSession
) -> Conversation:
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")

    result = await db.execute(
        select(Conversation)
        .where(
            Conversation.id == cid,
            Conversation.tenant_id == user.tenant_id,
            Conversation.deleted_at.is_(None),
        )
        .options(selectinload(Conversation.messages))
    )
    convo = result.scalar_one_or_none()
    if convo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    return convo

@router.get("", response_model=List[FEConversationListItem])
async def list_conversations(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[FEConversationListItem]:
    """Every stored conversation for this tenant (soft-deleted ones excluded) —
    bot-resolved and human-handled alike. unresolved_reason still gets set
    on handoff (auto low-confidence, or /escalate) so it's available as a
    signal/filter elsewhere, but it no longer hides anything from this list."""
    result = await db.execute(
        select(Conversation)
        .where(
            Conversation.tenant_id == current_user.tenant_id,
            Conversation.deleted_at.is_(None),
        )
        .options(selectinload(Conversation.messages))
        .order_by(Conversation.created_at.desc())
        .limit(200)
    )
    return [_list_item(c) for c in result.scalars().all()]

@router.get("/{conversation_id}", response_model=FEConversationDetail)
async def get_conversation(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEConversationDetail:
    convo = await _get_conversation_or_404(conversation_id, current_user, db)
    return FEConversationDetail(
        **_list_item(convo).model_dump(),
        messages=[_message(m) for m in convo.messages],
    )

class FEUpdateStatusRequest(BaseModel):
    status: Literal["Open", "Handed off", "Resolved"]

@router.put("/{conversation_id}/status", response_model=FEConversationListItem)
async def update_status(
    conversation_id: str,
    body: FEUpdateStatusRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEConversationListItem:
    """
    Manually set a conversation's status — lets an agent mark a chat Resolved
    (or reopen it) from the Conversations page, which feeds the resolution-rate
    metric on the Overview page.
    """
    convo = await _get_conversation_or_404(conversation_id, current_user, db)
    convo.status = body.status
    if body.status == "Resolved":
        convo.unresolved_reason = None
    await db.flush()
    return _list_item(convo)

@router.post("/{conversation_id}/reply", response_model=FESendReplyResponse)
async def send_reply(
    conversation_id: str,
    body: FESendReplyRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FESendReplyResponse:
    convo = await _get_conversation_or_404(conversation_id, current_user, db)

    reply_text = body.text.strip()
    msg = ConversationMessage(
        conversation_id=convo.id,
        role="agent",
        text=reply_text,
    )
    db.add(msg)
    if convo.status == "Open":
        convo.status = "Handed off"
    await db.flush()
    await db.refresh(msg)

    # Keyed off actual presence (is_visitor_still_present), not is_live —
    # is_live only means the owner formally clicked "Join chat"; a visitor
    # can be sitting right there watching (still polling) without that ever
    # having happened, and would see this reply appear live either way (see
    # WidgetPage.tsx's polling, which now refreshes history on every tick
    # while escalated, not only once is_live). Emailing them too in that
    # case would just be a redundant, unnecessary email.
    if convo.visitor_email and not helpers.is_visitor_still_present(convo):
        tenant = await helpers.get_tenant(current_user, db)
        fire_and_forget(notify_visitor_of_reply(
            convo.visitor_email, reply_text, tenant.workspace_name,
            current_user.email, current_user.full_name,
        ))

    return FESendReplyResponse(message=_message(msg))

@router.post("/{conversation_id}/resolve", response_model=FEConversationDetail)
async def resolve_conversation(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEConversationDetail:
    convo = await _get_conversation_or_404(conversation_id, current_user, db)
    convo.status = "Resolved"
    convo.is_live = False
    await db.flush()
    return FEConversationDetail(
        **_list_item(convo).model_dump(),
        messages=[_message(m) for m in convo.messages],
    )

@router.post("/{conversation_id}/join", response_model=FEConversationDetail)
async def join_conversation(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEConversationDetail:
    """Owner clicks "Join chat" on a conversation the visitor is waiting on
    live. Idempotent — joining an already-live conversation is a no-op, not
    an error (handles a double-click or two open tabs)."""
    convo = await _get_conversation_or_404(conversation_id, current_user, db)
    if convo.status == "Resolved":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Can't join a resolved conversation.",
        )
    if not convo.is_live:
        # Refuse to go "live" against a chat the visitor no longer has open
        # — see helpers.is_visitor_still_present. Skipped once already live
        # (idempotent re-join shouldn't suddenly fail because the visitor's
        # tab has since gone quiet mid-conversation).
        if not helpers.is_visitor_still_present(convo):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This visitor no longer appears to have the chat open.",
            )
        convo.is_live = True
        if convo.status == "Open":
            convo.status = "Handed off"
        await db.flush()
    return FEConversationDetail(
        **_list_item(convo).model_dump(),
        messages=[_message(m) for m in convo.messages],
    )

@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft delete — sets deleted_at rather than removing the row, so it's
    recoverable for CONVERSATION_TRASH_RETENTION_DAYS (see conversation_service
    .trash_purge_loop, which hard-deletes it after that window)."""
    convo = await _get_conversation_or_404(conversation_id, current_user, db)
    convo.deleted_at = datetime.now(timezone.utc)
    await db.flush()

@router.post("/purge-old-messages", response_model=FEPurgeMessagesResponse)
async def purge_old_messages(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEPurgeMessagesResponse:
    """Manual "clean up old messages" action (Settings -> Workspace) — see
    conversation_service.purge_old_message_text for exactly what qualifies."""
    purged = await purge_old_message_text(current_user.tenant_id, db)
    return FEPurgeMessagesResponse(purged=purged)
