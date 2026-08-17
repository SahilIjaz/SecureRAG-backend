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
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.frontend import helpers
from app.core.rate_limit import limiter
from app.database import AsyncSessionLocal, get_db
from app.models.chatbot_config import ChatbotConfig
from app.models.user import User
from app.schemas.frontend import (
    FEChatbotConfig,
    FERegenerateApiKeyResponse,
    FESaveChatbotConfigResponse,
)
from app.services.auth_service import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chatbot", tags=["Frontend — Chatbot"])

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
    record.config = body.model_dump()
    await db.flush()
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

class FETestChatResponse(BaseModel):
    reply: str
    sources: List[str] = []
    handoff: bool = False
    confidence: Optional[int] = None

@router.post("/test-chat", response_model=FETestChatResponse)
@limiter.limit("20/minute")
async def test_chat(
    request: Request,
    body: FETestChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FETestChatResponse:
    """
    Answer a question from the tenant's indexed knowledge base — powers the
    dashboard's "Test chatbot" modal. Runs the RAG pipeline (embed the
    question → search Pinecone → generate with Claude) and counts against
    the monthly question quota.
    """
    from app.services.rag_service import answer_question

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
        usage.questions_used = (usage.questions_used or 0) + 1
        await db.flush()

    if not result.get("sources"):
        # Nothing relevant in the knowledge base — reply with the configured fallback.
        return FETestChatResponse(reply=fallback, sources=[], confidence=result.get("confidence"))

    # Behavior tab: "Hand off to human" — escalate when the bot's self-reported
    # confidence falls below the configured threshold.
    confidence = result.get("confidence")
    threshold = behavior.get("confidenceThreshold", 60)
    if behavior.get("handoffToHuman") and confidence is not None and confidence < threshold:
        return FETestChatResponse(
            reply=fallback,
            sources=[],
            handoff=True,
            confidence=confidence,
        )

    return FETestChatResponse(
        reply=result["answer"], sources=result["sources"], confidence=confidence
    )

def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}

@router.post("/test-chat/stream")
@limiter.limit("20/minute")
async def test_chat_stream(
    request: Request,
    body: FETestChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """
    Streaming counterpart to /test-chat — same quota/config/handoff
    semantics, but the answer is delivered token-by-token over SSE instead
    of as one JSON response. Kept alongside the non-streaming endpoint
    (unused by the dashboard after this ships, but useful for debugging and
    mock-mode parity).
    """
    from app.services.rag_service import answer_question_stream

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

    # Plain locals only past this point — no ORM object may cross into the
    # generator below, which runs after this request's DB session is closed
    # (FastAPI tears down Depends(get_db) before a StreamingResponse body runs).
    tenant_id = current_user.tenant_id
    history = [m.model_dump() for m in body.history]
    message = body.message

    async def event_stream():
        succeeded = False
        try:
            async for ev in answer_question_stream(
                tenant_id=str(tenant_id), query=message, config=config,
                doc_names=doc_names, history=history,
            ):
                if ev["type"] == "token":
                    yield _sse("token", {"text": ev["text"]})
                elif ev["type"] == "final":
                    succeeded = True
                    yield _sse("done", {
                        "reply": ev["answer"], "sources": ev["sources"],
                        "handoff": ev["handoff"], "confidence": ev["confidence"],
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
                        await session.commit()
                except Exception:
                    logger.exception("Failed to record usage after streamed test chat")

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=SSE_HEADERS)

@router.post("/api-key/regenerate", response_model=FERegenerateApiKeyResponse)
async def regenerate_api_key(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FERegenerateApiKeyResponse:
    record = await _get_or_create_config(current_user, db)
    new_key = helpers.generate_widget_api_key()
    # Reassign (don't mutate) so SQLAlchemy detects the JSONB change.
    config = dict(record.config)
    deploy = dict(config.get("deploy", {}))
    deploy["apiKey"] = new_key
    config["deploy"] = deploy
    record.config = config
    await db.flush()
    return FERegenerateApiKeyResponse(apiKey=new_key)
