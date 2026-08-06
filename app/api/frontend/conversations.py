"""
Frontend-compat conversations endpoints (/api/conversations/...).

  - GET  /api/conversations             -> ConversationListItem[]
  - GET  /api/conversations/{id}        -> ConversationDetail (with messages)
  - POST /api/conversations/{id}/reply  -> {message}  (human agent reply; marks it Handed off)
"""

import uuid
from typing import List, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.frontend import helpers
from app.database import get_db
from app.models.conversation import Conversation, ConversationMessage
from app.models.user import User
from app.schemas.frontend import (
    FEConversationDetail,
    FEConversationListItem,
    FEConversationMessage,
    FESendReplyRequest,
    FESendReplyResponse,
)
from app.services.auth_service import get_current_user

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
    )

def _message(msg: ConversationMessage) -> FEConversationMessage:
    return FEConversationMessage(
        id=str(msg.id),
        role=msg.role,
        text=msg.text,
        time=helpers.hhmm(msg.created_at),
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
        .where(Conversation.id == cid, Conversation.tenant_id == user.tenant_id)
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
    result = await db.execute(
        select(Conversation)
        .where(Conversation.tenant_id == current_user.tenant_id)
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

    msg = ConversationMessage(
        conversation_id=convo.id,
        role="agent",
        text=body.text.strip(),
    )
    db.add(msg)
    if convo.status == "Open":
        convo.status = "Handed off"
    await db.flush()
    await db.refresh(msg)

    return FESendReplyResponse(message=_message(msg))
