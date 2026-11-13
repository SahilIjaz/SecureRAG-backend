"""
Audit trail, compliance evidence and response traces (/api/audit, /api/traces).

  GET /api/audit                      admin+  paginated, filterable entries
  GET /api/audit/actions              admin+  distinct action names (filter options)
  GET /api/audit/verify               admin+  recompute the tenant's hash chain
  GET /api/audit/compliance           admin+  evidence summary (controls, retention, integrity)
  GET /api/audit/export?format=csv    owner   full export (itself audited)
  GET /api/traces/{trace_id}          admin+  one answer trace
  GET /api/traces?conversationId=…    admin+  traces for a conversation

Everything is tenant-scoped through the caller's membership; an id from
another tenant is a 404, never a 403 (no existence leak).
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.frontend import helpers
from app.config import settings
from app.core.audit_middleware import client_ip
from app.core.rate_limit import limiter
from app.core.rbac import get_current_role, require_admin, require_owner
from app.database import get_db
from app.models.tenant_user import TenantRole
from app.models.user import User
from app.services import audit_service, trace_service

router = APIRouter(tags=["Frontend — Audit & traceability"])


# ── Schemas (mirror Nexus-frontend src/types) ────────────────────────────────

class FEAuditEntry(BaseModel):
    id: str
    createdAt: str
    actorEmail: Optional[str] = None
    actorRole: Optional[str] = None
    action: str
    resourceType: Optional[str] = None
    resourceId: Optional[str] = None
    resourceLabel: Optional[str] = None
    outcome: Literal["success", "failure"]
    statusCode: Optional[int] = None
    method: Optional[str] = None
    path: Optional[str] = None
    ip: Optional[str] = None
    userAgent: Optional[str] = None
    requestId: Optional[str] = None
    details: Optional[dict] = None
    entryHash: str
    prevHash: Optional[str] = None
    seq: Optional[int] = None

class FEAuditPage(BaseModel):
    items: List[FEAuditEntry]
    nextCursor: Optional[int] = None  # seq of the last row; pass back as ?cursor=

class FEAuditVerification(BaseModel):
    valid: bool
    entries: int
    checked: int
    brokenAt: Optional[str] = None
    reason: Optional[str] = None
    checkedAt: str

class FEComplianceControl(BaseModel):
    id: str
    label: str
    status: Literal["enabled", "disabled"]
    evidence: str

class FEComplianceSummary(BaseModel):
    entriesTotal: int
    failuresTotal: int
    oldestEntryAt: Optional[str] = None
    newestEntryAt: Optional[str] = None
    auditRetentionDays: int
    traceRetentionDays: int
    integrity: FEAuditVerification
    controls: List[FEComplianceControl]

class FETraceChunk(BaseModel):
    documentId: Optional[str] = None
    documentName: Optional[str] = None
    chunkId: Optional[str] = None
    sequence: Optional[int] = None
    page: Optional[int] = None
    score: Optional[float] = None
    rerankScore: Optional[float] = None
    snippet: str

class FEAnswerTrace(BaseModel):
    id: str
    createdAt: str
    channel: str
    conversationId: Optional[str] = None
    messageId: Optional[str] = None
    question: str
    answer: str
    decision: str
    confidence: Optional[int] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    promptTokens: Optional[int] = None
    completionTokens: Optional[int] = None
    totalTokens: Optional[int] = None
    latencyMs: Optional[int] = None
    retrieval: Optional[dict] = None
    chunks: List[FETraceChunk] = []
    conflicts: List[dict] = []

class FETracePage(BaseModel):
    items: List[FEAnswerTrace]
    nextCursor: Optional[str] = None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return helpers._as_aware(dt).isoformat() if dt else None


def _fe_entry(row) -> FEAuditEntry:
    return FEAuditEntry(
        id=str(row.id), createdAt=_iso(row.created_at), actorEmail=row.actor_email, actorRole=row.actor_role,
        action=row.action, resourceType=row.resource_type, resourceId=row.resource_id,
        resourceLabel=row.resource_label, outcome=row.outcome, statusCode=row.status_code,
        method=row.method, path=row.path, ip=row.ip, userAgent=row.user_agent, requestId=row.request_id,
        details=row.details, entryHash=row.entry_hash, prevHash=row.prev_hash, seq=row.seq,
    )


def _fe_trace(row) -> FEAnswerTrace:
    return FEAnswerTrace(
        id=str(row.id), createdAt=_iso(row.created_at), channel=row.channel,
        conversationId=str(row.conversation_id) if row.conversation_id else None,
        messageId=str(row.message_id) if row.message_id else None,
        question=row.question, answer=row.answer, decision=row.decision, confidence=row.confidence,
        provider=row.provider, model=row.model, promptTokens=row.prompt_tokens,
        completionTokens=row.completion_tokens, totalTokens=row.total_tokens, latencyMs=row.latency_ms,
        retrieval=row.retrieval, chunks=[FETraceChunk(**c) for c in (row.chunks or [])],
        conflicts=row.conflicts or [],
    )


def _parse_dt(value: Optional[str], name: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"{name} must be an ISO-8601 datetime.")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── Audit log ────────────────────────────────────────────────────────────────

@router.get("/audit", response_model=FEAuditPage)
async def list_audit(
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[int] = Query(None, ge=0),
    action: Optional[str] = Query(None, max_length=64),
    actor: Optional[str] = Query(None, max_length=320),
    outcome: Optional[Literal["success", "failure"]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> FEAuditPage:
    rows, next_cursor = await audit_service.list_entries(
        db, current_user.tenant_id, limit=limit, cursor=cursor, action=action,
        actor=actor, outcome=outcome, since=_parse_dt(since, "since"), until=_parse_dt(until, "until"),
    )
    return FEAuditPage(items=[_fe_entry(r) for r in rows], nextCursor=next_cursor)


@router.get("/audit/actions", response_model=List[str])
async def list_audit_actions(
    current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db),
) -> List[str]:
    return await audit_service.distinct_actions(db, current_user.tenant_id)


@router.get("/audit/verify", response_model=FEAuditVerification)
@limiter.limit("10/minute")
async def verify_audit_chain(
    request: Request,
    current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db),
) -> FEAuditVerification:
    v = await audit_service.verify_chain(db, current_user.tenant_id)
    return FEAuditVerification(**{**v, "checkedAt": _iso(v["checkedAt"])})


def _controls() -> List[FEComplianceControl]:
    def c(id_, label, enabled, evidence):
        return FEComplianceControl(id=id_, label=label, status="enabled" if enabled else "disabled", evidence=evidence)
    return [
        c("rbac", "Role-based access control", True, "owner / admin / agent roles enforced per request (app/core/rbac.py)"),
        c("auth", "Authenticated sessions with revocation", True, "JWT access tokens with jti revocation and token_version invalidation on password change"),
        c("tenant_isolation", "Tenant isolation", True, "every query filters on tenant_id; vector search scoped to the tenant's Pinecone namespace"),
        c("encryption_at_rest", "Uploaded files encrypted at rest", bool(settings.FILE_ENCRYPTION_KEY),
          "Fernet encryption before Cloudinary storage" + (" + post-quantum KEM wrapping" if settings.PQ_FILE_ENCRYPTION_ENABLED else "")),
        c("audit_trail", "Append-only audit trail", settings.AUDIT_ENABLED,
          "every mutating request recorded; UPDATE/DELETE rejected by database trigger; per-tenant SHA-256 hash chain"),
        c("traceability", "Response traceability", True, "every reply stores retrieved evidence, scores, confidence, decision and model"),
        c("retention", "Retention policy", settings.AUDIT_RETENTION_DAYS > 0,
          f"audit {settings.AUDIT_RETENTION_DAYS} days, traces {settings.TRACE_RETENTION_DAYS} days, purged by a daily sweep"),
        c("export", "Audit export", True, "CSV/JSON export of the full audit trail (owner only, itself audited)"),
        c("erasure", "Workspace deletion", True, "DELETE /api/settings/workspace deactivates the tenant and its users"),
    ]


@router.get("/audit/compliance", response_model=FEComplianceSummary)
async def compliance_summary(
    current_user: User = Depends(require_admin), db: AsyncSession = Depends(get_db),
) -> FEComplianceSummary:
    st = await audit_service.stats(db, current_user.tenant_id)
    v = await audit_service.verify_chain(db, current_user.tenant_id)
    return FEComplianceSummary(
        entriesTotal=st["total"], failuresTotal=st["failures"],
        oldestEntryAt=_iso(st["oldest"]), newestEntryAt=_iso(st["newest"]),
        auditRetentionDays=settings.AUDIT_RETENTION_DAYS, traceRetentionDays=settings.TRACE_RETENTION_DAYS,
        integrity=FEAuditVerification(**{**v, "checkedAt": _iso(v["checkedAt"])}),
        controls=_controls(),
    )


@router.get("/audit/export")
@limiter.limit("5/minute")
async def export_audit(
    request: Request,
    format: Literal["csv", "json"] = "csv",
    since: Optional[str] = None,
    until: Optional[str] = None,
    current_user: User = Depends(require_owner),
    role: TenantRole = Depends(get_current_role),
    db: AsyncSession = Depends(get_db),
):
    """Owner-only full export. Reads are not audited in general, but an
    export is a disclosure of the whole trail, so it is recorded explicitly."""
    rows = await audit_service.export_rows(
        db, current_user.tenant_id, since=_parse_dt(since, "since"), until=_parse_dt(until, "until"),
    )
    rows = list(rows)
    await audit_service.record(
        action="audit.export", tenant_id=current_user.tenant_id, actor_user_id=current_user.id,
        actor_email=current_user.email, actor_role=role.value if hasattr(role, "value") else str(role),
        resource_type="audit_log", method="GET", path=str(request.url.path), ip=client_ip(request),
        user_agent=request.headers.get("user-agent"), details={"format": format, "rows": len(rows)},
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    # The export is capped (audit_service.EXPORT_ROW_LIMIT); say so instead
    # of silently truncating — the client surfaces this header.
    extra = {"X-Export-Rows": str(len(rows)), "X-Export-Row-Limit": str(audit_service.EXPORT_ROW_LIMIT),
             "X-Export-Truncated": "1" if len(rows) >= audit_service.EXPORT_ROW_LIMIT else "0",
             "Access-Control-Expose-Headers": "Content-Disposition, X-Export-Rows, X-Export-Row-Limit, X-Export-Truncated"}
    if format == "json":
        body = json.dumps([audit_service.export_dict(r) for r in rows], indent=2, default=str)
        return StreamingResponse(
            io.BytesIO(body.encode("utf-8")), media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="nexus-audit-{stamp}.json"', **extra},
        )
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=audit_service.EXPORT_COLUMNS)
    writer.writeheader()
    for r in rows:
        d = audit_service.export_dict(r)
        d["details"] = json.dumps(d["details"], default=str) if d["details"] is not None else ""
        writer.writerow(d)
    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode("utf-8")), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="nexus-audit-{stamp}.csv"', **extra},
    )


# ── Answer traces ────────────────────────────────────────────────────────────

@router.get("/traces", response_model=FETracePage)
async def list_traces(
    conversationId: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = None,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> FETracePage:
    conv_id = None
    if conversationId:
        try:
            conv_id = uuid.UUID(conversationId)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    rows, next_cursor = await trace_service.list_traces(
        db, current_user.tenant_id, conversation_id=conv_id, limit=limit, cursor=_parse_dt(cursor, "cursor"),
    )
    return FETracePage(items=[_fe_trace(r) for r in rows], nextCursor=_iso(next_cursor))


@router.get("/traces/{trace_id}", response_model=FEAnswerTrace)
async def get_trace(
    trace_id: str,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> FEAnswerTrace:
    try:
        tid = uuid.UUID(trace_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trace not found.")
    row = await trace_service.get_trace(db, current_user.tenant_id, tid)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trace not found.")
    return _fe_trace(row)
