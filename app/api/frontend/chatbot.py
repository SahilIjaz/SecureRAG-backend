"""
Frontend-compat chatbot endpoints (/api/chatbot/...).

  - GET  /api/chatbot/config             -> ChatbotConfig (created with defaults on first read)
  - PUT  /api/chatbot/config             -> {success, config}
  - POST /api/chatbot/api-key/regenerate -> {apiKey}

The whole config is stored as one JSONB blob in the dashboard's wire format
(see app/models/chatbot_config.py).
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.api.frontend import helpers
from app.database import get_db
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
limiter = Limiter(key_func=get_remote_address)

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

class FETestChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    # The dashboard preview sends its current (possibly unsaved) draft config so
    # Behavior/Identity edits apply immediately, before "Save changes".
    config: Optional[FEChatbotConfig] = None

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

    quota = await helpers.get_quota(current_user.tenant_id, db)
    usage = await helpers.get_current_usage(current_user.tenant_id, db)
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

    record = await _get_or_create_config(current_user, db)
    config = body.config.model_dump() if body.config else record.config
    identity = config.get("identity", {})
    behavior = config.get("behavior", {})
    fallback = identity.get("fallbackMessage", "I'm not sure about that yet.")

    # Map document ids → filenames so answers can cite sources by name.
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
