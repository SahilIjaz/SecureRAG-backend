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

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, EmailStr, Field
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.frontend import helpers
from app.config import settings
from app.core.background import fire_and_forget
from app.core.rate_limit import limiter
from app.core.subscription_guard import is_subscription_usable
from app.schemas.frontend import CitationSource
from app.core.widget_auth import create_widget_session_token, decode_widget_session_token, is_origin_allowed
from app.database import AsyncSessionLocal, get_db
from app.models.chatbot_config import ChatbotConfig
from app.models.conversation import Conversation, ConversationMessage
from app.models.document import Document
from app.models.tenant import Tenant
from app.schemas.frontend import FEChatbotAppearance, FEChatbotIdentity
from app.services.notification_service import notify_tenant, conversation_alert_copy
from app.services.rag_service import store_conversation_classification

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Public — Widget"])

def _widget_key_or_ip(request: Request) -> str:
    """Rate-limit key: prefer the widget key (so a leaked key is capped no
    matter how many IPs it's used from) — fall back to IP for requests
    missing it entirely (they'll 401 anyway, but still shouldn't be free
    to hammer the endpoint)."""
    key = request.headers.get("x-widget-key")
    return f"widget:{key}" if key else get_remote_address(request)

async def _resolve_tenant_by_key(key: str, request: Request, db: AsyncSession) -> tuple[Tenant, ChatbotConfig]:
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

async def _resolve_tenant(request: Request, db: AsyncSession) -> tuple[Tenant, ChatbotConfig]:
    return await _resolve_tenant_by_key(request.headers.get("x-widget-key", ""), request, db)

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

def _should_start_fresh(conversation: Conversation) -> bool:
    """Whether the next message should open a NEW conversation rather than
    continue this one — true if it's already resolved, or if it has been idle
    longer than WIDGET_CONVERSATION_IDLE_MINUTES. A live/handed-off conversation
    is never split (a human may be mid-reply)."""
    if conversation.is_live or conversation.status == "Handed off":
        return False
    if conversation.status == "Resolved":
        return True
    last_activity = None
    if conversation.messages:
        last_activity = max((m.created_at for m in conversation.messages), default=None)
    last_activity = last_activity or conversation.visitor_last_seen_at
    if last_activity is None:
        return False
    if last_activity.tzinfo is None:
        last_activity = last_activity.replace(tzinfo=timezone.utc)
    idle = datetime.now(timezone.utc) - last_activity
    return idle > timedelta(minutes=settings.WIDGET_CONVERSATION_IDLE_MINUTES)

LiveState = Literal["not_escalated", "connecting", "live", "unavailable"]

def _compute_live_state(
    conversation: Conversation, tenant: Tenant, anyone_online: bool
) -> LiveState:
    """
    Single source of truth for what the widget's connect/wait UI should show
    — see NexusContext/LIVE_AGENT_HANDOFF_PLAN.md §3. Deliberately stateless:
    re-derived from current DB values every call rather than cached, so a
    late owner join is always picked up on the next poll no matter how long
    "unavailable" was already showing (edge case #5 in that doc).

    `anyone_online` is whether any team member (owner or agent) is currently
    around — computed by the caller via helpers.is_any_member_online.
    """
    if conversation.is_live:
        return "live"
    if conversation.status != "Handed off":
        return "not_escalated"
    if not anyone_online:
        return "unavailable"
    if conversation.live_wait_started_at is None:
        return "unavailable"
    waited = datetime.now(timezone.utc) - conversation.live_wait_started_at
    if waited < timedelta(seconds=settings.LIVE_JOIN_TIMEOUT_SECONDS):
        return "connecting"
    return "unavailable"

# ── GET /config ──────────────────────────────────────────────────────────────

class WidgetConfigResponse(BaseModel):
    identity: FEChatbotIdentity
    appearance: FEChatbotAppearance
    collectEmailBeforeChat: bool
    collectNameBeforeChat: bool
    collectPhoneBeforeChat: bool
    showSources: bool
    # Quick-reply menu the widget renders as tappable chips (empty if none).
    menu: list = []

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
        menu=config.get("menu", []) or [],
    )

# ── POST /message ────────────────────────────────────────────────────────────

class WidgetLeadInfo(BaseModel):
    name: Optional[str] = Field(None, max_length=255)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, max_length=32)

class WidgetMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    sessionToken: Optional[str] = None
    lead: Optional[WidgetLeadInfo] = None

class WidgetMessageResponse(BaseModel):
    reply: str
    sources: List[CitationSource] = []
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

    t0 = time.monotonic()
    tenant, config_record = await _resolve_tenant(request, db)
    t_auth = time.monotonic()
    config = config_record.config
    behavior = config.get("behavior", {})
    identity = config.get("identity", {})
    fallback = identity.get("fallbackMessage", "I'm not sure about that yet.")

    session_payload = decode_widget_session_token(body.sessionToken) if body.sessionToken else None
    conversation: Optional[Conversation] = None
    if session_payload and session_payload["tenant_id"] == str(tenant.id):
        conversation = await _load_conversation(session_payload["conversation_id"], tenant.id, db)
        # Auto-split: don't append to a conversation that's already resolved or
        # has gone idle — start a fresh one instead. This stops unrelated chats
        # (and, on a shared device, different visitors) from being merged, and
        # avoids reopening a conversation the owner already closed.
        if conversation is not None and _should_start_fresh(conversation):
            conversation = None
    t_conv = time.monotonic()

    # Snapshot prior turns from an existing conversation *before* possibly
    # creating a new one below — a brand-new Conversation has no history by
    # definition, and touching .messages on it post-flush would hit an
    # unloaded relationship, triggering an implicit lazy-load that
    # AsyncSession can't service outside an explicit await (MissingGreenlet).
    # Only a conversation that actually came from _load_conversation's
    # selectinload is safe to read .messages from directly.
    history = [{"role": m.role, "text": m.text} for m in conversation.messages] if conversation is not None else []

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
            # Explicit, not relied on as a column default: with the flush
            # deferred (see below), code further down reads .status before
            # any flush runs — mapped_column(default=...) only applies at
            # flush time, so an implicit default would leave this None until
            # then and silently break the `conversation.status == "Open"`
            # handoff check for a conversation's very first message.
            status="Open",
        )
        db.add(conversation)
        # No flush here: conversation.id (a client-side uuid4 default) isn't
        # populated until the INSERT actually runs, so the user-message
        # insert below is attached via the `conversation` relationship
        # rather than `conversation_id=conversation.id` — that lets
        # SQLAlchemy's unit-of-work order both INSERTs correctly within the
        # *single* flush() at t_flush, instead of paying for two separate
        # round trips to Neon on every session-opening message.
    t_newconv = time.monotonic()
    conversation.visitor_last_seen_at = datetime.now(timezone.utc)

    db.add(ConversationMessage(conversation=conversation, role="user", text=body.message.strip()))

    quota, usage = await helpers.get_quota_and_usage(tenant.id, db)
    t_quota = time.monotonic()
    quota_exceeded = (
        quota is not None
        and usage is not None
        and quota.max_questions_per_month != -1
        and usage.questions_used >= quota.max_questions_per_month
    )

    # Owner's subscription lapsed → degrade to the fallback message (same soft
    # path as quota exhaustion) rather than answering and burning LLM cost.
    subscription = await helpers.get_subscription(tenant.id, db)
    if not await is_subscription_usable(subscription, db):
        quota_exceeded = True

    unresolved_reason = None
    notif_type = None
    should_classify = False
    # Already escalated (auto low-confidence handoff, or a future manual
    # "talk to a human" trigger) — a human is expected to answer this, so
    # the bot must not generate — or get billed quota for — a reply to it.
    # Deliberately keyed off `status`, not `is_live`: status flips to
    # "Handed off" the moment escalation starts, well before an owner may
    # have actively joined, so gating on is_live alone would let the bot
    # keep answering during that gap. Not persisted as a ConversationMessage
    # (unlike the quota_exceeded reply below) — it's a transient UI
    # acknowledgement, not a real reply, and persisting it on every message
    # sent while waiting would spam the transcript.
    already_handed_off = conversation.status == "Handed off"

    if already_handed_off:
        reply = "Thanks for the extra detail — a teammate will pick this up and reply here shortly."
        sources, handoff = [], True
        t_docs = t_gen_start = t_gen_done = time.monotonic()
    elif quota_exceeded:
        reply, sources, handoff = fallback, [], False
        t_docs = t_gen_start = t_gen_done = time.monotonic()
    else:
        doc_names: dict = {}
        if behavior.get("showSources", True):
            docs_result = await db.execute(
                select(Document.id, Document.original_filename).where(
                    Document.tenant_id == tenant.id, Document.is_active == True
                )
            )
            doc_names = {str(doc_id): name for doc_id, name in docs_result.all()}
        t_docs = time.monotonic()

        try:
            t_gen_start = time.monotonic()
            result = await answer_question(
                tenant_id=str(tenant.id), query=body.message, config=config, doc_names=doc_names,
                history=history,
            )
            t_gen_done = time.monotonic()
        except Exception:
            logger.exception("Widget chat failed for tenant %s", tenant.id)
            reply, sources, handoff = fallback, [], False
            t_gen_done = time.monotonic()
        else:
            if usage is not None:
                await helpers.increment_questions_used(tenant.id, db)

            should_classify = True
            confidence = result.get("confidence")
            threshold = behavior.get("confidenceThreshold", 60)
            if not result.get("sources"):
                reply, sources, handoff = fallback, [], False
                unresolved_reason = "No matching document found"
                notif_type = "knowledge_gaps_detected"
            elif behavior.get("handoffToHuman") and confidence is not None and confidence < threshold:
                reply, sources, handoff = fallback, [], True
                unresolved_reason = "Low-confidence answer, handed off"
                notif_type = "unresolved_conversations"
            else:
                reply, sources, handoff = result["answer"], result["sources"], False

    if not already_handed_off:
        # Via the relationship, not conversation_id=conversation.id — for a
        # brand-new conversation .id is still None here (no flush has run
        # yet; see the comment above where it's constructed).
        db.add(ConversationMessage(conversation=conversation, role="bot", text=reply))
    if handoff and conversation.status == "Open":
        conversation.status = "Handed off"
        conversation.live_wait_started_at = datetime.now(timezone.utc)
    if unresolved_reason:
        conversation.unresolved_reason = unresolved_reason
    await db.flush()
    t_flush = time.monotonic()

    if notif_type:
        await notify_tenant(
            tenant.id, notif_type,
            *conversation_alert_copy(notif_type, body.message),
            f"/dashboard/conversations?id={conversation.id}", db,
        )

    if should_classify:
        fire_and_forget(store_conversation_classification(conversation.id, tenant.id, body.message, reply))

    # Re-issued on every message so an active conversation's sliding window
    # never expires mid-use, while an abandoned/leaked token still dies
    # within WIDGET_SESSION_TOKEN_TTL_HOURS of the last real activity.
    session_token = create_widget_session_token(str(tenant.id), str(conversation.id))

    logger.info(
        "widget /message timing tenant=%s auth=%.0fms conv_load=%.0fms new_conv=%.0fms quota=%.0fms "
        "docs=%.0fms generate=%.0fms final_flush=%.0fms total=%.0fms",
        tenant.id, (t_auth - t0) * 1000, (t_conv - t_auth) * 1000, (t_newconv - t_conv) * 1000,
        (t_quota - t_newconv) * 1000, (t_docs - t_quota) * 1000, (t_gen_done - t_gen_start) * 1000,
        (t_flush - t_gen_done) * 1000, (t_flush - t0) * 1000,
    )

    return WidgetMessageResponse(reply=reply, sources=sources, handoff=handoff, sessionToken=session_token)

def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}

@router.post("/message/stream")
@limiter.limit("20/minute", key_func=_widget_key_or_ip)
async def post_widget_message_stream(
    request: Request,
    body: WidgetMessageRequest,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """
    Streaming counterpart to /message — identical quota/lead-gating/
    handoff/persistence semantics, delivered token-by-token over SSE. A
    visitor never sees a raw error: any failure before the first token
    degrades to the tenant's configured fallback, exactly like /message's
    `except Exception` today.
    """
    from app.services.rag_service import answer_question_stream

    t0 = time.monotonic()
    tenant, config_record = await _resolve_tenant(request, db)
    t_auth = time.monotonic()
    config = config_record.config
    behavior = config.get("behavior", {})
    identity = config.get("identity", {})
    fallback = identity.get("fallbackMessage", "I'm not sure about that yet.")

    session_payload = decode_widget_session_token(body.sessionToken) if body.sessionToken else None
    conversation: Optional[Conversation] = None
    if session_payload and session_payload["tenant_id"] == str(tenant.id):
        conversation = await _load_conversation(session_payload["conversation_id"], tenant.id, db)
        # Auto-split: don't append to a conversation that's already resolved or
        # has gone idle — start a fresh one instead. This stops unrelated chats
        # (and, on a shared device, different visitors) from being merged, and
        # avoids reopening a conversation the owner already closed.
        if conversation is not None and _should_start_fresh(conversation):
            conversation = None
    t_conv = time.monotonic()

    # See post_widget_message()'s identical comment: only a conversation
    # loaded via _load_conversation's selectinload is safe to read
    # .messages from directly — a freshly constructed one isn't.
    history = [{"role": m.role, "text": m.text} for m in conversation.messages] if conversation is not None else []

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
            # Explicit, not relied on as a column default: with the flush
            # deferred (see below), code further down reads .status before
            # any flush runs — mapped_column(default=...) only applies at
            # flush time, so an implicit default would leave this None until
            # then and silently break the `conversation.status == "Open"`
            # handoff check for a conversation's very first message.
            status="Open",
        )
        db.add(conversation)
        # No flush here — see post_widget_message()'s identical comment:
        # the user-message insert below is attached via the `conversation`
        # relationship so both INSERTs land in the one flush() at t_flush
        # instead of two separate round trips to Neon.
    t_newconv = time.monotonic()
    conversation.visitor_last_seen_at = datetime.now(timezone.utc)

    db.add(ConversationMessage(conversation=conversation, role="user", text=body.message.strip()))

    quota, usage = await helpers.get_quota_and_usage(tenant.id, db)
    t_quota = time.monotonic()
    quota_exceeded = (
        quota is not None
        and usage is not None
        and quota.max_questions_per_month != -1
        and usage.questions_used >= quota.max_questions_per_month
    )

    # Owner's subscription lapsed → degrade to the fallback message (same soft
    # path as quota exhaustion) rather than answering and burning LLM cost.
    subscription = await helpers.get_subscription(tenant.id, db)
    if not await is_subscription_usable(subscription, db):
        quota_exceeded = True

    # See post_widget_message()'s identical comment: already escalated (auto
    # low-confidence handoff, or a future manual "talk to a human" trigger)
    # means a human is expected to answer, so the bot must not generate — or
    # get billed quota for — a reply. Keyed off `status`, not `is_live`, for
    # the same reason given there.
    already_handed_off = conversation.status == "Handed off"

    doc_names: dict = {}
    if not quota_exceeded and not already_handed_off and behavior.get("showSources", True):
        docs_result = await db.execute(
            select(Document.id, Document.original_filename).where(
                Document.tenant_id == tenant.id, Document.is_active == True
            )
        )
        doc_names = {str(doc_id): name for doc_id, name in docs_result.all()}
    t_docs = time.monotonic()

    await db.flush()
    t_flush = time.monotonic()

    # conversation.id (client-side uuid4 default) is only populated once the
    # flush above actually runs the INSERT — computed after it, unlike
    # before this change, when an earlier separate flush made it available
    # sooner but cost an extra round trip.
    session_token = create_widget_session_token(str(tenant.id), str(conversation.id))

    # Plain locals only past this point — no ORM object may cross into the
    # generator (the request's DB session is closed before its body runs).
    tenant_id = tenant.id
    conversation_id = conversation.id
    message = body.message

    logger.info(
        "widget /message/stream prep timing tenant=%s auth=%.0fms conv_load=%.0fms new_conv=%.0fms quota=%.0fms "
        "docs=%.0fms flush=%.0fms prep_total=%.0fms",
        tenant_id, (t_auth - t0) * 1000, (t_conv - t_auth) * 1000, (t_newconv - t_conv) * 1000,
        (t_quota - t_newconv) * 1000, (t_docs - t_quota) * 1000, (t_flush - t_docs) * 1000, (t_flush - t0) * 1000,
    )

    async def event_stream():
        t_stream_start = time.monotonic()
        yield _sse("session", {"sessionToken": session_token})

        final_text = fallback
        handoff = False
        count_usage = False
        unresolved_reason = None
        notif_type = None

        try:
            if already_handed_off:
                ack = "Thanks for the extra detail — a teammate will pick this up and reply here shortly."
                yield _sse("token", {"text": ack})
                yield _sse("done", {"reply": ack, "sources": [], "handoff": True, "sessionToken": session_token})
                final_text = ack
                handoff = True
            elif quota_exceeded:
                yield _sse("token", {"text": fallback})
                yield _sse("done", {"reply": fallback, "sources": [], "handoff": False, "sessionToken": session_token})
            else:
                streamed_any = False
                try:
                    async for ev in answer_question_stream(
                        tenant_id=str(tenant_id), query=message, config=config,
                        doc_names=doc_names, history=history,
                    ):
                        if ev["type"] == "token":
                            streamed_any = True
                            yield _sse("token", {"text": ev["text"]})
                        elif ev["type"] == "final":
                            final_text = ev["answer"]
                            handoff = ev["handoff"]
                            count_usage = True
                            # Check handoff *before* sources: answer_question_stream()
                            # deliberately blanks sources to [] for a suppressed
                            # low-confidence answer too, not just a genuine "nothing
                            # retrieved" — sources alone can't tell the two apart here.
                            if handoff:
                                unresolved_reason = "Low-confidence answer, handed off"
                                notif_type = "unresolved_conversations"
                            elif not ev["sources"]:
                                unresolved_reason = "No matching document found"
                                notif_type = "knowledge_gaps_detected"
                            yield _sse("done", {
                                "reply": ev["answer"], "sources": ev["sources"],
                                "handoff": ev["handoff"], "sessionToken": session_token,
                            })
                        elif ev["type"] == "error":
                            if streamed_any:
                                # Partial text already shown — keep it, tell the
                                # client the rest didn't arrive, but never show a
                                # visitor a raw error.
                                yield _sse("error", {"message": "stream_interrupted"})
                            else:
                                yield _sse("token", {"text": fallback})
                                yield _sse("done", {
                                    "reply": fallback, "sources": [], "handoff": False, "sessionToken": session_token,
                                })
                            return
                except Exception:
                    logger.exception("Widget chat stream failed for tenant %s", tenant_id)
                    yield _sse("token", {"text": fallback})
                    yield _sse("done", {"reply": fallback, "sources": [], "handoff": False, "sessionToken": session_token})
        finally:
            logger.info(
                "widget /message/stream time_to_done=%.0fms tenant=%s",
                (time.monotonic() - t_stream_start) * 1000, tenant_id,
            )

        t_persist_start = time.monotonic()
        try:
            async with AsyncSessionLocal() as session:
                # Not persisted for the already-handed-off ack — see
                # post_widget_message()'s identical comment: it's a
                # transient UI acknowledgement, not a real reply, and would
                # spam the transcript if stored on every message sent while
                # waiting for a human.
                if not already_handed_off:
                    session.add(ConversationMessage(conversation_id=conversation_id, role="bot", text=final_text or fallback))
                if handoff:
                    await session.execute(
                        update(Conversation)
                        .where(Conversation.id == conversation_id, Conversation.status == "Open")
                        .values(status="Handed off", live_wait_started_at=func.now())
                    )
                if unresolved_reason:
                    await session.execute(
                        update(Conversation)
                        .where(Conversation.id == conversation_id)
                        .values(unresolved_reason=unresolved_reason)
                    )
                if count_usage:
                    await helpers.increment_questions_used(tenant_id, session)
                if notif_type:
                    await notify_tenant(
                        tenant_id, notif_type,
                        *conversation_alert_copy(notif_type, message),
                        f"/dashboard/conversations?id={conversation_id}", session,
                    )
                await session.commit()
            if count_usage:
                fire_and_forget(store_conversation_classification(conversation_id, tenant_id, message, final_text or fallback))
        except Exception:
            logger.exception("Failed to persist widget reply after streaming (conversation %s)", conversation_id)
        finally:
            logger.info(
                "widget /message/stream persist=%.0fms tenant=%s",
                (time.monotonic() - t_persist_start) * 1000, tenant_id,
            )

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=SSE_HEADERS)

# ── POST /escalate ───────────────────────────────────────────────────────────

# Shown once per escalation, not repeated on every message while
# waiting/live — the persistent status banner covers the rest. Persisted as
# a real ConversationMessage (unlike the transient already_handed_off ack in
# /message) so it survives a reload and shows up in the dashboard inbox too.
_ESCALATE_ACK_TEXT = "Our team will get back to you shortly."

class WidgetEscalateRequest(BaseModel):
    sessionToken: str = Field(..., min_length=1)

class WidgetEscalateMessage(BaseModel):
    id: str
    role: str
    text: str
    createdAt: str

class WidgetEscalateResponse(BaseModel):
    state: LiveState
    message: Optional[WidgetEscalateMessage] = None
    # Tells the widget whether to show the "want us to notify you?" prompt
    # right away instead of waiting for the "unavailable" fallback — no
    # point asking again if the pre-chat form (or an earlier escalate call
    # in this same conversation) already collected one.
    hasContactInfo: bool = False

@router.post("/escalate", response_model=WidgetEscalateResponse)
@limiter.limit("10/minute", key_func=_widget_key_or_ip)
async def post_widget_escalate(
    request: Request,
    body: WidgetEscalateRequest,
    db: AsyncSession = Depends(get_db),
) -> WidgetEscalateResponse:
    """Visitor clicked "Talk to a human". Marks the conversation Handed off
    (if it wasn't already) and (re)starts the LIVE_JOIN_TIMEOUT_SECONDS
    countdown the widget's connecting UI polls against."""
    tenant, _ = await _resolve_tenant(request, db)

    payload = decode_widget_session_token(body.sessionToken)
    if not payload or payload["tenant_id"] != str(tenant.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")
    conversation = await _load_conversation(payload["conversation_id"], tenant.id, db)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")

    now = datetime.now(timezone.utc)
    conversation.visitor_last_seen_at = now
    if conversation.status != "Handed off":
        conversation.status = "Handed off"
    conversation.live_wait_started_at = now
    if not conversation.unresolved_reason:
        # So this shows up in the dashboard's Conversations list at all —
        # that list only surfaces conversations the bot didn't resolve on
        # its own, see api/frontend/conversations.py:list_conversations.
        conversation.unresolved_reason = "Visitor requested a human"

    ack_message: Optional[WidgetEscalateMessage] = None
    already_acked = any(m.role == "bot" and m.text == _ESCALATE_ACK_TEXT for m in conversation.messages)
    if not already_acked:
        ack = ConversationMessage(conversation_id=conversation.id, role="bot", text=_ESCALATE_ACK_TEXT)
        db.add(ack)
        await db.flush()
        await db.refresh(ack)
        ack_message = WidgetEscalateMessage(
            id=str(ack.id), role=ack.role, text=ack.text,
            createdAt=helpers._as_aware(ack.created_at).isoformat(),
        )
    await db.flush()

    last_user_message = next(
        (m.text for m in reversed(conversation.messages) if m.role == "user"),
        "A visitor asked to talk to a human.",
    )
    await notify_tenant(
        tenant.id, "unresolved_conversations",
        *conversation_alert_copy("unresolved_conversations", last_user_message),
        f"/dashboard/conversations?id={conversation.id}", db,
    )

    return WidgetEscalateResponse(
        state=_compute_live_state(
            conversation, tenant, await helpers.is_any_member_online(tenant.id, db)
        ),
        message=ack_message,
        hasContactInfo=bool(conversation.visitor_email or conversation.visitor_phone),
    )

# ── GET /live-status ─────────────────────────────────────────────────────────

class WidgetLiveStatusResponse(BaseModel):
    state: LiveState
    hasContactInfo: bool = False

@router.get("/live-status", response_model=WidgetLiveStatusResponse)
@limiter.limit("30/minute", key_func=_widget_key_or_ip)
async def get_widget_live_status(
    request: Request,
    sessionToken: str,
    db: AsyncSession = Depends(get_db),
) -> WidgetLiveStatusResponse:
    """Polled by the widget while connecting/live — see _compute_live_state.
    Also the main heartbeat behind helpers.is_visitor_still_present: this is
    what runs continuously while the tab is open and waiting/live, so it's
    the most reliable "the visitor is still here" signal available."""
    tenant, _ = await _resolve_tenant(request, db)

    payload = decode_widget_session_token(sessionToken)
    if not payload or payload["tenant_id"] != str(tenant.id):
        return WidgetLiveStatusResponse(state="not_escalated")
    conversation = await _load_conversation(payload["conversation_id"], tenant.id, db)
    if conversation is None:
        return WidgetLiveStatusResponse(state="not_escalated")

    conversation.visitor_last_seen_at = datetime.now(timezone.utc)
    await db.flush()

    return WidgetLiveStatusResponse(
        state=_compute_live_state(
            conversation, tenant, await helpers.is_any_member_online(tenant.id, db)
        ),
        hasContactInfo=bool(conversation.visitor_email or conversation.visitor_phone),
    )

# ── POST /contact ────────────────────────────────────────────────────────────

class WidgetContactRequest(BaseModel):
    sessionToken: str = Field(..., min_length=1)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, max_length=32)

@router.post("/contact", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/minute", key_func=_widget_key_or_ip)
async def post_widget_contact(
    request: Request,
    body: WidgetContactRequest,
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Optional contact info offered only at the "unavailable" fallback moment
    (see NexusContext/LIVE_AGENT_HANDOFF_PLAN.md §4) — never required, never
    asked upfront. Only fills in what's actually missing: never overwrites
    what the pre-chat lead form (or an earlier call to this same endpoint)
    already collected.
    """
    tenant, _ = await _resolve_tenant(request, db)

    payload = decode_widget_session_token(body.sessionToken)
    if not payload or payload["tenant_id"] != str(tenant.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")
    conversation = await _load_conversation(payload["conversation_id"], tenant.id, db)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")

    if body.email and not conversation.visitor_email:
        conversation.visitor_email = body.email
    phone = (body.phone or "").strip()
    if phone and not conversation.visitor_phone:
        conversation.visitor_phone = phone
    await db.flush()

# ── POST /leave ──────────────────────────────────────────────────────────────

class WidgetLeaveRequest(BaseModel):
    # Every other widget route takes its key via the X-Widget-Key header —
    # this one carries it in the body instead, because it's fired from
    # navigator.sendBeacon() on page unload, which can't set custom headers.
    apiKey: str = Field(..., min_length=1)
    sessionToken: str = Field(..., min_length=1)

@router.post("/leave", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute", key_func=_widget_key_or_ip)
async def post_widget_leave(
    request: Request,
    body: WidgetLeaveRequest,
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Fired via sendBeacon when the visitor's tab actually closes or
    navigates away (WidgetPage's `pagehide` handler) — clears
    visitor_last_seen_at immediately so the owner's "Visitor active" badge
    (helpers.is_visitor_still_present) flips to "not active" right away
    instead of only after VISITOR_PRESENCE_WINDOW_SECONDS of silence.
    Best-effort and silent on any failure: a beacon fired during unload has
    no one listening for a response, so there's nothing to do with an error
    besides swallow it — worst case, presence just falls back to the normal
    staleness timeout.
    """
    try:
        tenant, _ = await _resolve_tenant_by_key(body.apiKey, request, db)
    except HTTPException:
        return
    payload = decode_widget_session_token(body.sessionToken)
    if not payload or payload["tenant_id"] != str(tenant.id):
        return
    conversation = await _load_conversation(payload["conversation_id"], tenant.id, db)
    if conversation is None:
        return
    conversation.visitor_last_seen_at = None
    await db.flush()

# ── GET /history ─────────────────────────────────────────────────────────────

class WidgetHistoryMessage(BaseModel):
    id: str
    role: str
    text: str
    createdAt: str

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
            WidgetHistoryMessage(
                id=str(m.id), role=m.role, text=m.text,
                createdAt=helpers._as_aware(m.created_at).isoformat(),
            )
            for m in conversation.messages
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
