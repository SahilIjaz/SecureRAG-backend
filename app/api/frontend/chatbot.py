"""
Frontend-compat chatbot endpoints (/api/chatbot/...).

  - GET  /api/chatbot/config             -> ChatbotConfig (created with defaults on first read)
  - PUT  /api/chatbot/config             -> {success, config}
  - POST /api/chatbot/api-key/regenerate -> {apiKey}

The whole config is stored as one JSONB blob in the dashboard's wire format
(see app/models/chatbot_config.py).
"""

import json
import logging
import uuid
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.frontend import helpers
from app.core.rate_limit import limiter
from app.database import AsyncSessionLocal, get_db
from app.models.chatbot_config import ChatbotConfig
from app.models.conversation import Conversation, ConversationMessage
from app.models.user import User
from app.schemas.frontend import (
    CitationSource,
    FEChatbotConfig,
    FERegenerateApiKeyResponse,
    FESaveChatbotConfigResponse,
)
from app.services.auth_service import get_current_user
from app.core.subscription_guard import require_active_subscription
from app.core.rbac import require_admin
from app.services.notification_service import notify_tenant

logger = logging.getLogger(__name__)

# Chatbot config/deploy/keys are owner/admin territory.
router = APIRouter(
    prefix="/chatbot",
    tags=["Frontend — Chatbot"],
    dependencies=[Depends(require_admin)],
)

def _default_config(workspace_name: str, slug: str) -> dict:
    return {
        "identity": {
            "name": f"{workspace_name} Assistant" if workspace_name else "Support Assistant",
            "avatarUrl": None,
            "welcomeMessage": "Hi! I'm here to help. Ask me anything about our product.",
            "persona": "friendly",
            "language": "en",
            "fallbackMessage": "I'm not sure about that yet — want me to connect you with a teammate?",
        },
        "behavior": {
            "handoffToHuman": True,
            "confidenceThreshold": 60,
            "collectEmailBeforeChat": False,
            "collectNameBeforeChat": False,
            "collectPhoneBeforeChat": False,
            "showSources": True,
            "stayOnTopic": True,
            "tone": "balanced",
            "maxResponseLength": "medium",
        },
        "appearance": {
            "accentColor": "#D97706",
            "bubblePosition": "bottom-right",
            "showPoweredBy": True,
            "widgetTheme": "light",
            "fontSize": "medium",
        },
        "deploy": {
            "status": "draft",
            "deployedDomain": "",
            "allowedDomains": "",
            "botSlug": slug or "my-bot",
            "apiKey": helpers.generate_widget_api_key(),
        },
    }

async def _get_or_create_config(user: User, db: AsyncSession) -> ChatbotConfig:
    result = await db.execute(
        select(ChatbotConfig).where(ChatbotConfig.tenant_id == user.tenant_id)
    )
    record = result.scalar_one_or_none()
    if record is None:
        tenant = await helpers.get_tenant(user, db)
        workspace = tenant.workspace_name if tenant.workspace_name != "__pending__" else ""
        record = ChatbotConfig(
            tenant_id=user.tenant_id,
            config=_default_config(workspace, tenant.slug),
        )
        db.add(record)
        await db.flush()
    return record

# Distinct from the real widget's "Widget"/"API" channels (see
# models/conversation.py's CONVERSATION_CHANNELS) so a tenant testing their
# own bot from the dashboard never gets confused with real visitor traffic
# in the Conversations inbox or Analytics.
_SIMULATED_CHANNEL = "Internal Test"

async def _get_or_create_simulated_conversation(
    tenant_id: uuid.UUID, conversation_id: Optional[str], db: AsyncSession
) -> Conversation:
    """
    "Simulate real visitor" mode's conversation — reused across every message
    in one Test Chatbot modal session (the frontend carries the id forward
    from each response), same idea as the widget's session-token-scoped
    conversation.
    """
    if conversation_id:
        try:
            cid = uuid.UUID(conversation_id)
        except ValueError:
            cid = None
        if cid is not None:
            result = await db.execute(
                select(Conversation).where(Conversation.id == cid, Conversation.tenant_id == tenant_id)
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                return existing

    convo = Conversation(tenant_id=tenant_id, visitor_name="Internal Test", channel=_SIMULATED_CHANNEL)
    db.add(convo)
    await db.flush()
    return convo

@router.get("/config", response_model=FEChatbotConfig)
async def get_config(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEChatbotConfig:
    record = await _get_or_create_config(current_user, db)
    return FEChatbotConfig.model_validate(record.config)

@router.put("/config", response_model=FESaveChatbotConfigResponse)
async def save_config(
    body: FEChatbotConfig,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FESaveChatbotConfigResponse:
    record = await _get_or_create_config(current_user, db)

    # The widget API key is server-generated and only rotated via the dedicated
    # regenerate endpoint — never trust a client-supplied value in the config
    # body (which would let a tenant set an arbitrary/duplicate key). Preserve
    # whatever key the record already has; the old cache entry is invalidated
    # under that same (unchanged) key.
    existing_key = (record.config or {}).get("deploy", {}).get("apiKey", "")
    new_config = body.model_dump()
    new_config.setdefault("deploy", {})["apiKey"] = existing_key

    # Menu is a paid feature — drop it on save if the plan doesn't include it,
    # so a lower tier can't persist (or serve) a menu.
    from app.core.entitlements import get_entitlements
    subscription = await helpers.get_subscription(current_user.tenant_id, db)
    if not get_entitlements(subscription).menu:
        new_config["menu"] = []
        body.menu = []

    record.config = new_config
    await db.flush()
    # The widget's tenant+config lookup caches on the API key (see
    # helpers.get_tenant_by_widget_api_key) — without this, an edit here
    # (allowedDomains, identity, behavior...) wouldn't reach the live widget
    # for up to WIDGET_TENANT_CACHE_TTL_SECONDS.
    helpers.invalidate_widget_tenant_cache(existing_key)
    # Reflect the preserved key back to the caller so the response matches
    # what was persisted.
    body.deploy.apiKey = existing_key
    return FESaveChatbotConfigResponse(success=True, config=body)

MAX_AVATAR_MB = 2
ALLOWED_AVATAR_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

class FEAvatarUploadResponse(BaseModel):
    avatarUrl: str

@router.post("/avatar", response_model=FEAvatarUploadResponse)
@limiter.limit("10/minute")
async def upload_avatar(
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEAvatarUploadResponse:
    """
    Upload the chatbot avatar image to Cloudinary and store the hosted URL in
    the chatbot config. Returns the URL for the frontend to show immediately.
    """
    from app.core.storage import upload_image_to_cloudinary

    if file.content_type not in ALLOWED_AVATAR_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Avatar must be a PNG, JPG, WebP, or GIF image.",
        )

    content = await file.read()
    if len(content) > MAX_AVATAR_MB * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Avatar must be at most {MAX_AVATAR_MB}MB.",
        )

    try:
        avatar_url = await upload_image_to_cloudinary(content, current_user.tenant_id)
    except Exception as e:
        logger.exception("Avatar upload failed for tenant %s", current_user.tenant_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Image upload failed: {e}",
        )

    # Persist onto the stored config too, so the avatar survives even if the
    # user navigates away without hitting "Save changes".
    record = await _get_or_create_config(current_user, db)
    config = dict(record.config)
    identity = dict(config.get("identity", {}))
    identity["avatarUrl"] = avatar_url
    config["identity"] = identity
    record.config = config
    await db.flush()

    return FEAvatarUploadResponse(avatarUrl=avatar_url)

class FETestChatHistoryMessage(BaseModel):
    role: Literal["user", "bot"]
    text: str = Field(..., min_length=1, max_length=4000)

class FETestChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    # The dashboard preview sends its current (possibly unsaved) draft config so
    # Behavior/Identity edits apply immediately, before "Save changes".
    config: Optional[FEChatbotConfig] = None
    # Test Chatbot is stateless (nothing persisted) — unlike the widget it
    # can't recover prior turns from the DB, so the modal replays them from
    # its local React state.
    history: List[FETestChatHistoryMessage] = Field(default_factory=list, max_length=20)
    # "Simulate real visitor" toggle in the Test Chatbot modal — when true,
    # this turn is persisted as a real Conversation (channel="Internal Test")
    # instead of staying ephemeral, so it shows up in the inbox/analytics and
    # can trigger the same handoff/knowledge-gap notifications a real visitor
    # message would. Quota consumption is unconditional either way (unchanged
    # from before this flag existed).
    simulateReal: bool = False
    # Set by the frontend from a prior response's conversationId once a
    # simulated session has started, so the whole modal-open session appends
    # to one Conversation instead of creating a new one per message.
    conversationId: Optional[str] = None

class FETestChatResponse(BaseModel):
    reply: str
    sources: List[CitationSource] = []
    handoff: bool = False
    confidence: Optional[int] = None
    conversationId: Optional[str] = None

@router.post("/test-chat", response_model=FETestChatResponse)
@limiter.limit("20/minute")
async def test_chat(
    request: Request,
    body: FETestChatRequest,
    current_user: User = Depends(require_active_subscription),
    db: AsyncSession = Depends(get_db),
) -> FETestChatResponse:
    """
    Answer a question from the tenant's indexed knowledge base — powers the
    dashboard's "Test chatbot" modal. Runs the RAG pipeline (embed the
    question → search Pinecone → generate with Claude) and counts against
    the monthly question quota.

    When body.simulateReal is set ("Simulate real visitor" toggle), this turn
    is additionally persisted as a real Conversation (channel="Internal
    Test") and can trigger the same handoff/knowledge-gap notifications and
    sentiment/topic classification a real widget visitor's message would —
    see _get_or_create_simulated_conversation and the public widget's
    identical branching in api/public/widget.py.
    """
    from app.services.rag_service import answer_question, store_conversation_classification
    from app.core.background import fire_and_forget
    from app.services.notification_service import notify_tenant, conversation_alert_copy

    quota, usage = await helpers.get_quota_and_usage(current_user.tenant_id, db)
    if (
        quota is not None
        and usage is not None
        and quota.max_questions_per_month != -1
        and usage.questions_used >= quota.max_questions_per_month
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Monthly message quota reached. Upgrade your plan to continue.",
        )

    # The dashboard always sends its current draft — skip the DB round trip
    # for the saved config entirely when it did (this is the hot path behind
    # every Test Chatbot message).
    if body.config:
        config = body.config.model_dump()
    else:
        record = await _get_or_create_config(current_user, db)
        config = record.config
    identity = config.get("identity", {})
    behavior = config.get("behavior", {})
    fallback = identity.get("fallbackMessage", "I'm not sure about that yet.")

    # Map document ids → filenames so answers can cite sources by name —
    # only needed when the tenant actually wants sources cited.
    doc_names: dict = {}
    if behavior.get("showSources", True):
        from app.models.document import Document

        docs_result = await db.execute(
            select(Document.id, Document.original_filename).where(
                Document.tenant_id == current_user.tenant_id, Document.is_active == True
            )
        )
        doc_names = {str(doc_id): name for doc_id, name in docs_result.all()}

    convo: Optional[Conversation] = None
    if body.simulateReal:
        convo = await _get_or_create_simulated_conversation(current_user.tenant_id, body.conversationId, db)
        db.add(ConversationMessage(conversation_id=convo.id, role="user", text=body.message.strip()))

    try:
        result = await answer_question(
            tenant_id=str(current_user.tenant_id),
            query=body.message,
            config=config,
            doc_names=doc_names,
            history=[m.model_dump() for m in body.history],
        )
    except Exception as e:
        logger.exception("Test chat failed for tenant %s", current_user.tenant_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Chat backend error: {e}",
        )

    if usage is not None:
        await helpers.increment_questions_used(current_user.tenant_id, db)

    confidence = result.get("confidence")
    threshold = behavior.get("confidenceThreshold", 60)
    unresolved_reason = None
    notif_type = None
    if not result.get("sources"):
        reply, sources, handoff = fallback, [], False
        unresolved_reason, notif_type = "No matching document found", "knowledge_gaps_detected"
    elif behavior.get("handoffToHuman") and confidence is not None and confidence < threshold:
        reply, sources, handoff = fallback, [], True
        unresolved_reason, notif_type = "Low-confidence answer, handed off", "unresolved_conversations"
    else:
        # Respect the tenant's showSources toggle for the citation data
        # itself, not just prompt labeling — previously this always leaked
        # raw source text to the response regardless of the flag.
        reply = result["answer"]
        sources = result["sources"] if behavior.get("showSources", True) else []
        handoff = False

    if convo is not None:
        db.add(ConversationMessage(
            conversation_id=convo.id, role="bot", text=reply, sources=sources or None,
        ))
        if handoff and convo.status == "Open":
            convo.status = "Handed off"
        if unresolved_reason:
            convo.unresolved_reason = unresolved_reason
        await db.flush()
        if notif_type:
            await notify_tenant(
                current_user.tenant_id, notif_type,
                *conversation_alert_copy(notif_type, body.message),
                f"/dashboard/conversations?id={convo.id}", db,
            )
        fire_and_forget(store_conversation_classification(convo.id, current_user.tenant_id, body.message, reply))

    return FETestChatResponse(
        reply=reply, sources=sources, handoff=handoff, confidence=confidence,
        conversationId=str(convo.id) if convo is not None else None,
    )

def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}

@router.post("/test-chat/stream")
@limiter.limit("20/minute")
async def test_chat_stream(
    request: Request,
    body: FETestChatRequest,
    current_user: User = Depends(require_active_subscription),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """
    Streaming counterpart to /test-chat — same quota/config/handoff/
    simulateReal semantics, but the answer is delivered token-by-token over
    SSE instead of as one JSON response. This is the one the dashboard's
    Test Chatbot modal actually calls; /test-chat is kept for debugging and
    mock-mode parity.
    """
    from app.services.rag_service import answer_question_stream, store_conversation_classification
    from app.core.background import fire_and_forget
    from app.services.notification_service import notify_tenant, conversation_alert_copy

    quota, usage = await helpers.get_quota_and_usage(current_user.tenant_id, db)
    if (
        quota is not None
        and usage is not None
        and quota.max_questions_per_month != -1
        and usage.questions_used >= quota.max_questions_per_month
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Monthly message quota reached. Upgrade your plan to continue.",
        )

    if body.config:
        config = body.config.model_dump()
    else:
        record = await _get_or_create_config(current_user, db)
        config = record.config
    behavior = config.get("behavior", {})

    doc_names: dict = {}
    if behavior.get("showSources", True):
        from app.models.document import Document

        docs_result = await db.execute(
            select(Document.id, Document.original_filename).where(
                Document.tenant_id == current_user.tenant_id, Document.is_active == True
            )
        )
        doc_names = {str(doc_id): name for doc_id, name in docs_result.all()}

    conversation_id: Optional[uuid.UUID] = None
    if body.simulateReal:
        convo = await _get_or_create_simulated_conversation(current_user.tenant_id, body.conversationId, db)
        db.add(ConversationMessage(conversation_id=convo.id, role="user", text=body.message.strip()))
        await db.flush()
        conversation_id = convo.id

    # Plain locals only past this point — no ORM object may cross into the
    # generator below, which runs after this request's DB session is closed
    # (FastAPI tears down Depends(get_db) before a StreamingResponse body runs).
    tenant_id = current_user.tenant_id
    history = [m.model_dump() for m in body.history]
    message = body.message

    async def event_stream():
        succeeded = False
        final_text = None
        final_sources: list = []
        unresolved_reason = None
        notif_type = None
        handoff = False
        try:
            async for ev in answer_question_stream(
                tenant_id=str(tenant_id), query=message, config=config,
                doc_names=doc_names, history=history,
            ):
                if ev["type"] == "token":
                    yield _sse("token", {"text": ev["text"]})
                elif ev["type"] == "final":
                    succeeded = True
                    final_text = ev["answer"]
                    handoff = ev["handoff"]
                    # Check handoff *before* sources: answer_question_stream()
                    # deliberately blanks sources to [] for a suppressed
                    # low-confidence answer too, not just a genuine "nothing
                    # retrieved" — sources alone can't tell the two apart here.
                    if handoff:
                        unresolved_reason, notif_type = "Low-confidence answer, handed off", "unresolved_conversations"
                    elif not ev["sources"]:
                        unresolved_reason, notif_type = "No matching document found", "knowledge_gaps_detected"
                    # Respect the tenant's showSources toggle for the citation
                    # data itself, same as the non-streaming handler.
                    final_sources = ev["sources"] if behavior.get("showSources", True) else []
                    yield _sse("done", {
                        "reply": ev["answer"], "sources": final_sources, "handoff": ev["handoff"],
                        "confidence": ev["confidence"],
                        "conversationId": str(conversation_id) if conversation_id else None,
                    })
                elif ev["type"] == "error":
                    yield _sse("error", {"message": f"Chat backend error: {ev['message']}"})
                    return
        except Exception as e:
            logger.exception("Test chat stream failed for tenant %s", tenant_id)
            yield _sse("error", {"message": f"Chat backend error: {e}"})
        finally:
            if succeeded:
                try:
                    async with AsyncSessionLocal() as session:
                        await helpers.increment_questions_used(tenant_id, session)
                        if conversation_id is not None:
                            session.add(ConversationMessage(
                                conversation_id=conversation_id, role="bot", text=final_text or "",
                                sources=final_sources or None,
                            ))
                            if handoff:
                                await session.execute(
                                    update(Conversation)
                                    .where(Conversation.id == conversation_id, Conversation.status == "Open")
                                    .values(status="Handed off")
                                )
                            if unresolved_reason:
                                await session.execute(
                                    update(Conversation)
                                    .where(Conversation.id == conversation_id)
                                    .values(unresolved_reason=unresolved_reason)
                                )
                            if notif_type:
                                await notify_tenant(
                                    tenant_id, notif_type,
                                    *conversation_alert_copy(notif_type, message),
                                    f"/dashboard/conversations?id={conversation_id}", session,
                                )
                        await session.commit()
                    if conversation_id is not None:
                        fire_and_forget(
                            store_conversation_classification(conversation_id, tenant_id, message, final_text or "")
                        )
                except Exception:
                    logger.exception("Failed to record usage/conversation after streamed test chat")

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=SSE_HEADERS)

@router.post("/api-key/regenerate", response_model=FERegenerateApiKeyResponse)
async def regenerate_api_key(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FERegenerateApiKeyResponse:
    record = await _get_or_create_config(current_user, db)
    old_key = record.config.get("deploy", {}).get("apiKey", "")
    new_key = helpers.generate_widget_api_key()
    # Reassign (don't mutate) so SQLAlchemy detects the JSONB change.
    config = dict(record.config)
    deploy = dict(config.get("deploy", {}))
    deploy["apiKey"] = new_key
    config["deploy"] = deploy
    record.config = config
    await db.flush()
    # Without this, a just-revoked key would keep authenticating widget
    # requests (via the stale cache entry from helpers.get_tenant_by_widget_
    # api_key) for up to WIDGET_TENANT_CACHE_TTL_SECONDS after regeneration —
    # defeats the point of rotating a leaked key.
    helpers.invalidate_widget_tenant_cache(old_key)
    return FERegenerateApiKeyResponse(apiKey=new_key)
