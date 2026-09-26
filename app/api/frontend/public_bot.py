"""
PUBLIC chatbot endpoints (/api/public/bot/...) — no authentication.

These power the hosted chat page at <frontend>/bot/{slug} that anyone can
open in a browser:

  GET  /api/public/bot/{slug}       -> public config (identity + appearance only)
  POST /api/public/bot/{slug}/chat  -> RAG answer; creates real Conversation records

The bot must be published (deploy.status == "live") — flipping the Deploy tab
toggle to Draft takes the public page offline. Visitor chats create real
conversations, so the dashboard's Conversations page and Overview metrics
reflect actual traffic. Each answer counts toward the monthly message quota.
"""

import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.api.frontend import helpers
from app.database import get_db
from app.models.chatbot_config import ChatbotConfig
from app.models.conversation import Conversation, ConversationMessage
from app.models.document import Document
from app.models.tenant import Tenant
from app.schemas.frontend import FEChatbotAppearance, FEChatbotIdentity
from app.services.classification_service import schedule_classification
from app.core.widget_auth import (
    create_widget_session_token,
    decode_widget_session_token,
)
from app.core.subscription_guard import ensure_subscription_active

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/public/bot", tags=["Public — Chatbot"])
limiter = Limiter(key_func=get_remote_address)


def _ip_and_slug(request: Request) -> str:
    """Rate-limit key combining caller IP and target bot slug, so one IP's
    budget is per-bot (can't spread abuse across many bots) and one bot can't
    have its budget exhausted by a single IP for every other visitor."""
    return f"{get_remote_address(request)}:{request.path_params.get('slug', '')}"

class FEPublicBotResponse(BaseModel):
    identity: FEChatbotIdentity
    appearance: FEChatbotAppearance
    collectEmailBeforeChat: bool

class FEPublicChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    # Signed session token from a previous reply — NOT a raw conversation id.
    # A raw client-supplied id would let anyone read/append to another visitor's
    # conversation by guessing UUIDs; the token binds the caller to exactly one
    # (tenant, conversation) pair and is tamper-proof.
    sessionToken: Optional[str] = None
    visitorName: Optional[str] = Field(None, max_length=255)
    visitorEmail: Optional[EmailStr] = None

class FEPublicChatResponse(BaseModel):
    reply: str
    # Opaque signed token the client must echo back on the next turn to stay in
    # the same conversation. Replaces the old plaintext conversationId.
    sessionToken: str
    handoff: bool = False

async def _get_live_bot(slug: str, db: AsyncSession) -> tuple:
    """Resolve slug -> (tenant, config dict). 404 if unknown, 403 if not published."""
    result = await db.execute(
        select(Tenant).where(Tenant.slug == slug, Tenant.is_active == True)
    )
    tenant = result.scalar_one_or_none()

    record = None
    if tenant is not None:
        result = await db.execute(
            select(ChatbotConfig).where(ChatbotConfig.tenant_id == tenant.id)
        )
        record = result.scalar_one_or_none()
    else:
        # Renaming the workspace changes tenant.slug but leaves the config's
        # botSlug behind, so links minted from the Deploy tab keep working.
        result = await db.execute(
            select(ChatbotConfig).where(
                ChatbotConfig.config["deploy"]["botSlug"].astext == slug
            )
        )
        record = result.scalar_one_or_none()
        if record is not None:
            tenant = (
                await db.execute(
                    select(Tenant).where(
                        Tenant.id == record.tenant_id, Tenant.is_active == True
                    )
                )
            ).scalar_one_or_none()

    if tenant is None or record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chatbot not found.")

    config = record.config
    if config.get("deploy", {}).get("status") != "live":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This chatbot is not published. Set its status to Live in the Deploy tab.",
        )
    return tenant, config

@router.get("/{slug}", response_model=FEPublicBotResponse)
@limiter.limit("60/minute")
async def get_public_bot(
    request: Request,
    slug: str,
    db: AsyncSession = Depends(get_db),
) -> FEPublicBotResponse:
    """Public config for rendering the chat page. Never exposes deploy/apiKey."""
    _, config = await _get_live_bot(slug, db)
    return FEPublicBotResponse(
        identity=FEChatbotIdentity.model_validate(config.get("identity", {})),
        appearance=FEChatbotAppearance.model_validate(config.get("appearance", {})),
        collectEmailBeforeChat=bool(config.get("behavior", {}).get("collectEmailBeforeChat")),
    )

@router.post("/{slug}/chat", response_model=FEPublicChatResponse)
@limiter.limit("15/minute", key_func=_ip_and_slug)
async def public_chat(
    request: Request,
    slug: str,
    body: FEPublicChatRequest,
    db: AsyncSession = Depends(get_db),
) -> FEPublicChatResponse:
    from app.services.rag_service import answer_question

    tenant, config = await _get_live_bot(slug, db)
    behavior = config.get("behavior", {})
    identity = config.get("identity", {})
    fallback = identity.get("fallbackMessage", "I'm not sure about that yet.")

    # Block chat when the owner's subscription has lapsed (flips expired lazily).
    subscription = await helpers.get_subscription(tenant.id, db)
    await ensure_subscription_active(subscription, db)

    # Monthly quota — visitor messages consume it like any other question.
    quota = await helpers.get_quota(tenant.id, db)
    usage = await helpers.get_current_usage(tenant.id, db)
    if (
        quota is not None
        and usage is not None
        and quota.max_questions_per_month != -1
        and usage.questions_used >= quota.max_questions_per_month
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This chatbot has reached its monthly message limit.",
        )

    # Find or create the visitor's conversation. The conversation is resolved
    # ONLY from a valid signed session token whose tenant matches this bot —
    # never from a raw client-supplied id. An invalid/expired/foreign token is
    # treated as "no session" and a fresh conversation is started.
    conversation = None
    payload = decode_widget_session_token(body.sessionToken)
    if payload and payload.get("tenant_id") == str(tenant.id):
        try:
            cid = uuid.UUID(payload["conversation_id"])
            result = await db.execute(
                select(Conversation).where(
                    Conversation.id == cid, Conversation.tenant_id == tenant.id
                )
            )
            conversation = result.scalar_one_or_none()
        except (ValueError, KeyError):
            conversation = None
        # Auto-split: a resolved conversation starts a fresh one (same rule as
        # the widget). Idle-based splitting isn't applied here because this
        # endpoint doesn't eager-load messages; resolution is the common case.
        if conversation is not None and conversation.status == "Resolved":
            conversation = None
    if conversation is None:
        conversation = Conversation(
            tenant_id=tenant.id,
            visitor_name=(body.visitorName or "Visitor").strip() or "Visitor",
            visitor_email=body.visitorEmail,
            status="Open",
            sentiment="Neutral",
            channel="Widget",
        )
        db.add(conversation)
        await db.flush()

    db.add(ConversationMessage(conversation_id=conversation.id, role="user", text=body.message.strip()))
    await db.flush()

    # Source-name map so answers can cite documents.
    docs_result = await db.execute(
        select(Document.id, Document.original_filename).where(
            Document.tenant_id == tenant.id, Document.is_active == True
        )
    )
    doc_names = {str(doc_id): name for doc_id, name in docs_result.all()}

    try:
        result = await answer_question(
            tenant_id=str(tenant.id),
            query=body.message,
            config=config,
            doc_names=doc_names,
        )
    except Exception:
        logger.exception("Public chat failed for slug %s", slug)
        # Still record the failure gracefully for the visitor.
        db.add(ConversationMessage(conversation_id=conversation.id, role="bot", text=fallback))
        await db.flush()
        await db.commit()
        session_token = create_widget_session_token(str(tenant.id), str(conversation.id))
        return FEPublicChatResponse(reply=fallback, sessionToken=session_token)

    if usage is not None:
        usage.questions_used = (usage.questions_used or 0) + 1

    handoff = False
    confidence = result.get("confidence")
    threshold = behavior.get("confidenceThreshold", 60)
    if not result.get("sources"):
        reply = fallback
        conversation.unresolved_reason = "No matching doc"
    elif behavior.get("handoffToHuman") and confidence is not None and confidence < threshold:
        reply = fallback
        handoff = True
        conversation.status = "Handed off"
        conversation.unresolved_reason = "Low confidence"
    else:
        reply = result["answer"]

    db.add(ConversationMessage(conversation_id=conversation.id, role="bot", text=reply))
    await db.flush()
    await db.commit()

    # Classify in the background so the dashboard's topic/sentiment/resolution
    # cards get real data without delaying the visitor's reply.
    schedule_classification(conversation.id)

    session_token = create_widget_session_token(str(tenant.id), str(conversation.id))
    return FEPublicChatResponse(
        reply=reply, sessionToken=session_token, handoff=handoff
    )
