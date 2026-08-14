"""
Public chat widget API — the endpoints a visitor's browser (or a direct
server-to-server integration) calls to actually chat with a tenant's bot.

Mounted as its own small ASGI sub-application (see `widget_app` at the
bottom) rather than included in the main `frontend_router`, for two reasons:

  1. Auth is completely different: every route here is keyed off the
     widget's public API key (`X-Widget-Key` header), resolved via
     `helpers.get_tenant_by_widget_api_key` — never `get_current_user`
     (dashboard JWT). The two are intentionally disjoint; a leaked widget
     key can never reach a dashboard/knowledge-base/settings/billing route.
  2. CORS is fundamentally different: the main app's CORSMiddleware has a
     static allow-list of dev origins. This surface is embedded on
     arbitrary customer domains that aren't known ahead of time, so it
     needs `allow_origins=["*"]` at the transport level — the *real*
     per-tenant domain allowlist (`deploy.allowedDomains`) is enforced
     inside the route handlers below via `is_origin_allowed`, which does an
     exact-hostname check, not the permissive wildcard CORS allowance.
"""

import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.frontend import helpers
from app.core.rate_limit import limiter
from app.core.widget_auth import create_widget_session_token, decode_widget_session_token, is_origin_allowed
from app.database import get_db
from app.models.chatbot_config import ChatbotConfig
from app.models.conversation import Conversation, ConversationMessage
from app.models.document import Document
from app.models.tenant import Tenant
from app.schemas.frontend import FEChatbotAppearance, FEChatbotIdentity

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Public — Widget"])

def _widget_key_or_ip(request: Request) -> str:
    """Rate-limit key: prefer the widget key (so a leaked key is capped no
    matter how many IPs it's used from) — fall back to IP for requests
    missing it entirely (they'll 401 anyway, but still shouldn't be free
    to hammer the endpoint)."""
    key = request.headers.get("x-widget-key")
    return f"widget:{key}" if key else get_remote_address(request)

async def _resolve_tenant(request: Request, db: AsyncSession) -> tuple[Tenant, ChatbotConfig]:
    key = request.headers.get("x-widget-key", "")
    resolved = await helpers.get_tenant_by_widget_api_key(key, db)
    if resolved is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid widget API key.")
    tenant, config_record = resolved

    allowed_domains = config_record.config.get("deploy", {}).get("allowedDomains", "")
    if not allowed_domains.strip():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This chatbot has no allowed domains configured yet.",
        )
    origin = request.headers.get("origin")
    if not is_origin_allowed(origin, allowed_domains):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This domain is not allowed to embed this chatbot.",
        )
    return tenant, config_record

async def _load_conversation(conversation_id: str, tenant_id: uuid.UUID, db: AsyncSession) -> Optional[Conversation]:
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        return None
    result = await db.execute(
        select(Conversation)
        .where(Conversation.id == cid, Conversation.tenant_id == tenant_id)
        .options(selectinload(Conversation.messages))
    )
    return result.scalar_one_or_none()

# ── GET /config ──────────────────────────────────────────────────────────────

class WidgetConfigResponse(BaseModel):
    identity: FEChatbotIdentity
    appearance: FEChatbotAppearance
    collectEmailBeforeChat: bool
    collectNameBeforeChat: bool
    collectPhoneBeforeChat: bool
    showSources: bool

@router.get("/config", response_model=WidgetConfigResponse)
@limiter.limit("30/minute", key_func=_widget_key_or_ip)
async def get_widget_config(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> WidgetConfigResponse:
    tenant, config_record = await _resolve_tenant(request, db)
    config = config_record.config
    behavior = config.get("behavior", {})
    return WidgetConfigResponse(
        identity=FEChatbotIdentity.model_validate(config.get("identity", {})),
        appearance=FEChatbotAppearance.model_validate(config.get("appearance", {})),
        collectEmailBeforeChat=behavior.get("collectEmailBeforeChat", False),
        collectNameBeforeChat=behavior.get("collectNameBeforeChat", False),
        collectPhoneBeforeChat=behavior.get("collectPhoneBeforeChat", False),
        showSources=behavior.get("showSources", True),
    )

# ── POST /message ────────────────────────────────────────────────────────────

class WidgetLeadInfo(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None

class WidgetMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    sessionToken: Optional[str] = None
    lead: Optional[WidgetLeadInfo] = None

class WidgetMessageResponse(BaseModel):
    reply: str
    sources: List[str] = []
    handoff: bool = False
    sessionToken: str

@router.post("/message", response_model=WidgetMessageResponse)
@limiter.limit("20/minute", key_func=_widget_key_or_ip)
async def post_widget_message(
    request: Request,
    body: WidgetMessageRequest,
    db: AsyncSession = Depends(get_db),
) -> WidgetMessageResponse:
    from app.services.rag_service import answer_question

    tenant, config_record = await _resolve_tenant(request, db)
    config = config_record.config
    behavior = config.get("behavior", {})
    identity = config.get("identity", {})
    fallback = identity.get("fallbackMessage", "I'm not sure about that yet.")

    session_payload = decode_widget_session_token(body.sessionToken) if body.sessionToken else None
    conversation: Optional[Conversation] = None
    if session_payload and session_payload["tenant_id"] == str(tenant.id):
        conversation = await _load_conversation(session_payload["conversation_id"], tenant.id, db)

    if conversation is None:
        lead = body.lead or WidgetLeadInfo()
        missing = []
        if behavior.get("collectEmailBeforeChat") and not (lead.email or "").strip():
            missing.append("email")
        if behavior.get("collectNameBeforeChat") and not (lead.name or "").strip():
            missing.append("name")
        if behavior.get("collectPhoneBeforeChat") and not (lead.phone or "").strip():
            missing.append("phone")
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Missing required field(s): {', '.join(missing)}",
            )
        conversation = Conversation(
            tenant_id=tenant.id,
            visitor_name=(lead.name or "Visitor").strip() or "Visitor",
            visitor_email=(lead.email or None),
            visitor_phone=(lead.phone or None),
            channel="Widget",
        )
        db.add(conversation)
        await db.flush()

    db.add(ConversationMessage(conversation_id=conversation.id, role="user", text=body.message.strip()))

    quota, usage = await helpers.get_quota_and_usage(tenant.id, db)
    quota_exceeded = (
        quota is not None
        and usage is not None
        and quota.max_questions_per_month != -1
        and usage.questions_used >= quota.max_questions_per_month
    )

    if quota_exceeded:
        reply, sources, handoff = fallback, [], False
    else:
        docs_result = await db.execute(
            select(Document.id, Document.original_filename).where(
                Document.tenant_id == tenant.id, Document.is_active == True
            )
        )
        doc_names = {str(doc_id): name for doc_id, name in docs_result.all()}

        try:
            result = await answer_question(
                tenant_id=str(tenant.id), query=body.message, config=config, doc_names=doc_names
            )
        except Exception:
            logger.exception("Widget chat failed for tenant %s", tenant.id)
            reply, sources, handoff = fallback, [], False
        else:
            if usage is not None:
                usage.questions_used = (usage.questions_used or 0) + 1

            confidence = result.get("confidence")
            threshold = behavior.get("confidenceThreshold", 60)
            if not result.get("sources"):
                reply, sources, handoff = fallback, [], False
            elif behavior.get("handoffToHuman") and confidence is not None and confidence < threshold:
                reply, sources, handoff = fallback, [], True
            else:
                reply, sources, handoff = result["answer"], result["sources"], False

    db.add(ConversationMessage(conversation_id=conversation.id, role="bot", text=reply))
    if handoff and conversation.status == "Open":
        conversation.status = "Handed off"
    await db.flush()

    # Re-issued on every message so an active conversation's sliding window
    # never expires mid-use, while an abandoned/leaked token still dies
    # within WIDGET_SESSION_TOKEN_TTL_HOURS of the last real activity.
    session_token = create_widget_session_token(str(tenant.id), str(conversation.id))

    return WidgetMessageResponse(reply=reply, sources=sources, handoff=handoff, sessionToken=session_token)

# ── GET /history ─────────────────────────────────────────────────────────────

class WidgetHistoryMessage(BaseModel):
    id: str
    role: str
    text: str

class WidgetHistoryResponse(BaseModel):
    messages: List[WidgetHistoryMessage] = []

@router.get("/history", response_model=WidgetHistoryResponse)
@limiter.limit("30/minute", key_func=_widget_key_or_ip)
async def get_widget_history(
    request: Request,
    sessionToken: str,
    db: AsyncSession = Depends(get_db),
) -> WidgetHistoryResponse:
    tenant, _ = await _resolve_tenant(request, db)

    payload = decode_widget_session_token(sessionToken)
    if not payload or payload["tenant_id"] != str(tenant.id):
        return WidgetHistoryResponse(messages=[])

    conversation = await _load_conversation(payload["conversation_id"], tenant.id, db)
    if conversation is None:
        return WidgetHistoryResponse(messages=[])

    return WidgetHistoryResponse(
        messages=[
            WidgetHistoryMessage(id=str(m.id), role=m.role, text=m.text) for m in conversation.messages
        ]
    )

# ── Sub-application (own CORS + rate-limit error handler) ───────────────────

widget_app = FastAPI(
    title="Widget Public API",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
widget_app.state.limiter = limiter

@widget_app.exception_handler(RateLimitExceeded)
async def _widget_rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded. Please try again later."})

widget_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Widget-Key"],
    max_age=3600,
)

widget_app.include_router(router)
