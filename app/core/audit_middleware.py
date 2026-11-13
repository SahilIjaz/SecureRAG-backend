"""
HTTP audit middleware: every mutating request to the dashboard/API surface
becomes one audit_logs row — actor (from the Bearer token), action (from the
matched route), resource (from the path, enriched by the handler when it
knows more), outcome (from the status code or a raised exception), IP and
user agent.

Handlers enrich an entry by setting, at any point before returning:

    request.state.audit = {
        "action": "document.delete",          # optional override
        "resource_type": "document",
        "resource_id": "<uuid>",
        "resource_label": "pricing.pdf",
        "details": {...},                      # small, JSON-serialisable
    }

What is NOT audited here (and why):
  * GET/HEAD/OPTIONS — reads; the audit trail is about changes. Reads of
    the audit log itself and exports are recorded explicitly by their
    endpoints.
  * /api/auth/login|signup|… — recorded explicitly inside those handlers,
    where the attempted email and the failure reason are known.
  * Visitor chat traffic (/api/public/*, test-chat, presence pings, widget
    leave beacons) — recorded as answer traces / usage logs instead; it is
    not an administrative action.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Optional

from fastapi import Request
from fastapi.routing import APIRoute
from jose import JWTError
from starlette.responses import Response

from app.config import settings
from app.core.security import decode_token
from app.services import audit_service

logger = logging.getLogger(__name__)

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

_SKIP_PREFIXES = (
    "/api/public/",
    "/api/presence",
    "/api/chatbot/test-chat",
    "/api/billing/webhook",
    "/api/auth/login",
    "/api/auth/google",
    "/api/auth/signup",
    "/api/auth/verify-otp",
    "/api/auth/resend-otp",
    "/api/auth/forgot-password",
    "/api/auth/reset-password",
    "/api/auth/accept-invite",
    "/api/auth/logout",
    "/api/user/password",
)

# Friendly names for the most common routes; anything else gets
# "<method> <route template>" (still fully searchable).
ACTION_NAMES = {
    ("POST", "/api/knowledge/documents"): "document.upload",
    ("DELETE", "/api/knowledge/documents/{document_id}"): "document.delete",
    ("PATCH", "/api/knowledge/documents/{document_id}/authority"): "document.authority.update",
    ("POST", "/api/knowledge/urls"): "url.add",
    ("POST", "/api/knowledge/faqs"): "faq.add",
    ("PATCH", "/api/knowledge/faqs/{faq_id}"): "faq.update",
    ("POST", "/api/knowledge/reindex"): "knowledge.reindex",
    ("PUT", "/api/chatbot/config"): "chatbot.config.update",
    ("POST", "/api/chatbot/avatar"): "chatbot.avatar.update",
    ("POST", "/api/chatbot/api-key/regenerate"): "chatbot.apikey.regenerate",
    ("PUT", "/api/settings/workspace"): "settings.workspace.update",
    ("DELETE", "/api/settings/workspace"): "settings.workspace.delete",
    ("PUT", "/api/settings/notifications"): "settings.notifications.update",
    ("PUT", "/api/settings/model"): "settings.model.update",
    ("POST", "/api/settings/billing/checkout"): "billing.checkout",
    ("POST", "/api/settings/billing/cancel"): "billing.cancel",
    ("PUT", "/api/settings/billing/plan"): "billing.plan.change",
    ("POST", "/api/settings/billing/payment-method/setup-intent"): "billing.payment_method.setup",
    ("POST", "/api/settings/billing/wallet/topup"): "billing.wallet.topup",
    ("POST", "/api/settings/api-keys"): "apikey.create",
    ("DELETE", "/api/settings/api-keys/{key_id}"): "apikey.delete",
    ("POST", "/api/settings/members/invite"): "team.invite",
    ("DELETE", "/api/settings/members/invites/{invite_id}"): "team.invite.revoke",
    ("DELETE", "/api/settings/members/{user_id}"): "team.member.remove",
    ("PUT", "/api/user/profile"): "user.profile.update",
    ("POST", "/api/user/avatar"): "user.avatar.update",
    ("PUT", "/api/conversations/{conversation_id}/status"): "conversation.status.update",
    ("POST", "/api/conversations/{conversation_id}/reply"): "conversation.reply",
    ("POST", "/api/conversations/{conversation_id}/resolve"): "conversation.resolve",
    ("POST", "/api/conversations/{conversation_id}/claim"): "conversation.claim",
    ("POST", "/api/conversations/{conversation_id}/transfer"): "conversation.transfer",
    ("POST", "/api/conversations/{conversation_id}/join"): "conversation.join",
    ("DELETE", "/api/conversations/{conversation_id}"): "conversation.delete",
    ("POST", "/api/conversations/purge-old-messages"): "conversation.purge",
    ("POST", "/api/onboarding/complete"): "onboarding.complete",
}

_RESOURCE_TYPES = {
    "document_id": "document", "faq_id": "faq", "key_id": "api_key", "invite_id": "invite",
    "user_id": "user", "conversation_id": "conversation",
}

_route_templates: dict = {}


def should_audit(method: str, path: str) -> bool:
    if method not in MUTATING or not path.startswith("/api/"):
        return False
    return not any(path.startswith(p) for p in _SKIP_PREFIXES)


def action_for(method: str, template: Optional[str], path: str) -> str:
    if template and (method, template) in ACTION_NAMES:
        return ACTION_NAMES[(method, template)]
    return f"{method.lower()} {template or re.sub(r'/[0-9a-f-]{36}', '/{id}', path)}"


def _template_for(request: Request) -> Optional[str]:
    """Route template ("/api/knowledge/documents/{document_id}") for the
    matched endpoint. Starlette 0.38 doesn't put the route in scope, so map
    endpoint → path once from the app's route table."""
    endpoint = request.scope.get("endpoint")
    if endpoint is None:
        return None
    if not _route_templates:
        for route in request.app.routes:
            if isinstance(route, APIRoute):
                _route_templates[route.endpoint] = route.path
    return _route_templates.get(endpoint)


def _actor_from_token(request: Request) -> tuple[Optional[str], Optional[str]]:
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None, None
    try:
        payload = decode_token(auth[7:].strip())
    except JWTError:
        return None, None
    if payload.get("type") != "access":
        return None, None
    return payload.get("sub"), payload.get("tenant_id")


def client_ip(request: Request) -> Optional[str]:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


async def _actor_details(user_id: Optional[str], tenant_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(email, role) for the actor — one cached user load + one role query."""
    if not user_id:
        return None, None
    try:
        from app.database import AsyncSessionLocal
        from app.models.tenant_user import TenantUser
        from app.models.user import User
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            email = (await db.execute(select(User.email).where(User.id == uuid.UUID(user_id)))).scalar_one_or_none()
            role = None
            if tenant_id:
                role = (await db.execute(
                    select(TenantUser.role).where(
                        TenantUser.tenant_id == uuid.UUID(tenant_id), TenantUser.user_id == uuid.UUID(user_id)
                    )
                )).scalar_one_or_none()
            return email, (role.value if hasattr(role, "value") else role) or ("owner" if email else None)
    except Exception:
        logger.exception("audit: actor lookup failed")
        return None, None


async def audit_middleware(request: Request, call_next):
    if not settings.AUDIT_ENABLED or not should_audit(request.method, request.url.path):
        return await call_next(request)

    request_id = str(uuid.uuid4())
    request.state.audit_request_id = request_id
    status_code: Optional[int] = None
    error: Optional[str] = None
    response: Optional[Response] = None
    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception as e:  # noqa: BLE001 — recorded, then re-raised unchanged
        error = type(e).__name__
        status_code = 500
        raise
    finally:
        try:
            await _record(request, request_id, status_code, error)
        except Exception:
            logger.exception("audit middleware: record failed")
    if response is not None:
        response.headers["X-Request-Id"] = request_id
    return response


async def _record(request: Request, request_id: str, status_code: Optional[int], error: Optional[str]) -> None:
    user_id, tenant_id = _actor_from_token(request)
    email, role = await _actor_details(user_id, tenant_id)
    template = _template_for(request)
    enrich = getattr(request.state, "audit", None) or {}
    params = request.path_params or {}
    resource_type = enrich.get("resource_type")
    resource_id = enrich.get("resource_id")
    if resource_type is None or resource_id is None:
        for key, value in params.items():
            if key in _RESOURCE_TYPES:
                resource_type = resource_type or _RESOURCE_TYPES[key]
                resource_id = resource_id or str(value)
                break
    details = dict(enrich.get("details") or {})
    if error:
        details["error"] = error
    await audit_service.record(
        action=enrich.get("action") or action_for(request.method, template, request.url.path),
        outcome="success" if status_code is not None and status_code < 400 else "failure",
        tenant_id=enrich.get("tenant_id") or tenant_id,
        actor_user_id=user_id,
        actor_email=enrich.get("actor_email") or email,
        actor_role=role,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_label=enrich.get("resource_label"),
        status_code=status_code,
        method=request.method,
        path=request.url.path,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        request_id=request_id,
        details=details or None,
    )
