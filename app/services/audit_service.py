"""
Audit trail: append-only, hash-chained, tenant-scoped.

    record(...)        — append one entry (own transaction, never raises to
                         the caller: an audit failure is logged loudly but
                         must not fail the user's request).
    list_entries(...)  — cursor-paginated, filterable read for the admin UI.
    verify_chain(...)  — recompute the per-tenant hash chain and report the
                         first broken link, if any.
    export_rows(...)   — flat rows for CSV/JSON export.
    purge_expired()    — retention sweep; the only code path permitted to
                         delete (the DB trigger checks `nexus.audit_purge`).

Hash chain: entry_hash = SHA-256(prev_hash || canonical JSON of the entry's
content). prev_hash is the entry_hash of the previous entry of the same
tenant (or null for the first). Appends take a per-tenant advisory lock for
the duration of the insert transaction so concurrent requests can't both
read the same "previous" row.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.audit_log import AuditLog

logger = logging.getLogger(__name__)

GLOBAL_CHAIN_KEY = "global"  # events with no tenant (unknown-email logins)


# ── Hashing (pure) ───────────────────────────────────────────────────────────

def canonical_content(entry: dict) -> str:
    """Deterministic serialisation of the fields that are covered by the
    hash. Field order is fixed by sort_keys; datetimes/UUIDs are rendered as
    strings so the Python object and the DB row hash identically."""
    covered = {
        "tenant_id": _s(entry.get("tenant_id")),
        "actor_user_id": _s(entry.get("actor_user_id")),
        "actor_email": entry.get("actor_email"),
        "actor_role": entry.get("actor_role"),
        "action": entry.get("action"),
        "resource_type": entry.get("resource_type"),
        "resource_id": _s(entry.get("resource_id")),
        "resource_label": entry.get("resource_label"),
        "outcome": entry.get("outcome"),
        "status_code": entry.get("status_code"),
        "method": entry.get("method"),
        "path": entry.get("path"),
        "ip": entry.get("ip"),
        "user_agent": entry.get("user_agent"),
        "request_id": entry.get("request_id"),
        "details": entry.get("details"),
        "created_at": _iso(entry.get("created_at")),
    }
    return json.dumps(covered, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(prev_hash: Optional[str], entry: dict) -> str:
    return hashlib.sha256(((prev_hash or "") + "|" + canonical_content(entry)).encode("utf-8")).hexdigest()


def _s(v: Any) -> Optional[str]:
    return None if v is None else str(v)


def _iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat(timespec="microseconds")
    return str(v)


def _row_dict(row: AuditLog) -> dict:
    return {
        "tenant_id": row.tenant_id, "actor_user_id": row.actor_user_id, "actor_email": row.actor_email,
        "actor_role": row.actor_role, "action": row.action, "resource_type": row.resource_type,
        "resource_id": row.resource_id, "resource_label": row.resource_label, "outcome": row.outcome,
        "status_code": row.status_code, "method": row.method, "path": row.path, "ip": row.ip,
        "user_agent": row.user_agent, "request_id": row.request_id, "details": row.details,
        "created_at": row.created_at,
    }


def _chain_key(tenant_id: Optional[uuid.UUID]) -> str:
    return str(tenant_id) if tenant_id else GLOBAL_CHAIN_KEY


# ── Writes ───────────────────────────────────────────────────────────────────

async def _append(db: AsyncSession, entry: dict) -> AuditLog:
    tenant_id = entry.get("tenant_id")
    # Per-tenant advisory lock (transaction-scoped) → the "previous row" read
    # below is stable until our insert commits.
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": "audit:" + _chain_key(tenant_id)})
    prev = (await db.execute(
        select(AuditLog.entry_hash)
        .where(AuditLog.tenant_id == tenant_id if tenant_id else AuditLog.tenant_id.is_(None))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(1)
    )).scalar_one_or_none()
    entry = {**entry, "created_at": entry.get("created_at") or datetime.now(timezone.utc)}
    row = AuditLog(**entry, prev_hash=prev, entry_hash=compute_hash(prev, entry))
    db.add(row)
    return row


async def record(
    *,
    action: str,
    outcome: str = "success",
    tenant_id: Optional[uuid.UUID | str] = None,
    actor_user_id: Optional[uuid.UUID | str] = None,
    actor_email: Optional[str] = None,
    actor_role: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    resource_label: Optional[str] = None,
    status_code: Optional[int] = None,
    method: Optional[str] = None,
    path: Optional[str] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_id: Optional[str] = None,
    details: Optional[dict] = None,
) -> Optional[uuid.UUID]:
    """Append one audit entry in its own transaction. Returns the new row id,
    or None if auditing is disabled or the write failed (logged)."""
    if not settings.AUDIT_ENABLED:
        return None
    entry = {
        "tenant_id": _uuid(tenant_id), "actor_user_id": _uuid(actor_user_id),
        "actor_email": (actor_email or None) and actor_email[:320],
        "actor_role": actor_role, "action": action[:64], "resource_type": resource_type,
        "resource_id": (resource_id or None) and str(resource_id)[:64],
        "resource_label": (resource_label or None) and str(resource_label)[:255],
        "outcome": "success" if outcome == "success" else "failure",
        "status_code": status_code, "method": method, "path": (path or None) and path[:255],
        "ip": (ip or None) and ip[:64], "user_agent": (user_agent or None) and user_agent[:255],
        "request_id": request_id, "details": details,
    }
    try:
        async with AsyncSessionLocal() as db:
            row = await _append(db, entry)
            await db.commit()
            return row.id
    except Exception:
        logger.exception("AUDIT WRITE FAILED action=%s tenant=%s actor=%s", action, tenant_id, actor_email)
        return None


def _uuid(v) -> Optional[uuid.UUID]:
    if v is None or v == "":
        return None
    try:
        return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
    except ValueError:
        return None


# ── Reads ────────────────────────────────────────────────────────────────────

async def list_entries(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    limit: int = 50,
    cursor: Optional[datetime] = None,
    action: Optional[str] = None,
    actor: Optional[str] = None,
    outcome: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> tuple[list[AuditLog], Optional[datetime]]:
    """Newest first. `cursor` is the created_at of the last row seen."""
    q = select(AuditLog).where(AuditLog.tenant_id == tenant_id)
    if cursor:
        q = q.where(AuditLog.created_at < cursor)
    if action:
        q = q.where(AuditLog.action.like(action.replace("*", "%")))
    if actor:
        q = q.where(AuditLog.actor_email.ilike(f"%{actor}%"))
    if outcome in ("success", "failure"):
        q = q.where(AuditLog.outcome == outcome)
    if since:
        q = q.where(AuditLog.created_at >= since)
    if until:
        q = q.where(AuditLog.created_at <= until)
    q = q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit + 1)
    rows = list((await db.execute(q)).scalars().all())
    next_cursor = rows[limit - 1].created_at if len(rows) > limit else None
    return rows[:limit], next_cursor


async def distinct_actions(db: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    rows = (await db.execute(
        select(AuditLog.action).where(AuditLog.tenant_id == tenant_id).distinct().order_by(AuditLog.action)
    )).scalars().all()
    return list(rows)


async def stats(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    total, oldest, newest, failures = (await db.execute(
        select(
            func.count(AuditLog.id), func.min(AuditLog.created_at), func.max(AuditLog.created_at),
            func.count(AuditLog.id).filter(AuditLog.outcome == "failure"),
        ).where(AuditLog.tenant_id == tenant_id)
    )).one()
    return {"total": int(total or 0), "oldest": oldest, "newest": newest, "failures": int(failures or 0)}


async def verify_chain(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """Recompute every hash for the tenant, oldest first. Detects edited
    content (hash mismatch), a broken link (prev_hash ≠ previous entry_hash)
    and, for the first entry, a non-null prev_hash."""
    rows = (await db.execute(
        select(AuditLog).where(AuditLog.tenant_id == tenant_id)
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
    )).scalars().all()
    prev: Optional[str] = None
    for i, row in enumerate(rows):
        if row.prev_hash != prev:
            return {"valid": False, "entries": len(rows), "checked": i, "brokenAt": str(row.id),
                    "reason": "previous-hash link does not match", "checkedAt": datetime.now(timezone.utc)}
        expected = compute_hash(prev, _row_dict(row))
        if row.entry_hash != expected:
            return {"valid": False, "entries": len(rows), "checked": i, "brokenAt": str(row.id),
                    "reason": "entry content does not match its hash", "checkedAt": datetime.now(timezone.utc)}
        prev = row.entry_hash
    return {"valid": True, "entries": len(rows), "checked": len(rows), "brokenAt": None,
            "reason": None, "checkedAt": datetime.now(timezone.utc)}


async def export_rows(
    db: AsyncSession, tenant_id: uuid.UUID, *, since: Optional[datetime] = None, until: Optional[datetime] = None,
    limit: int = 50_000,
) -> Iterable[AuditLog]:
    q = select(AuditLog).where(AuditLog.tenant_id == tenant_id)
    if since:
        q = q.where(AuditLog.created_at >= since)
    if until:
        q = q.where(AuditLog.created_at <= until)
    q = q.order_by(AuditLog.created_at.asc(), AuditLog.id.asc()).limit(limit)
    return (await db.execute(q)).scalars().all()


EXPORT_COLUMNS = [
    "id", "created_at", "tenant_id", "actor_user_id", "actor_email", "actor_role", "action",
    "resource_type", "resource_id", "resource_label", "outcome", "status_code", "method", "path",
    "ip", "user_agent", "request_id", "details", "prev_hash", "entry_hash",
]


def export_dict(row: AuditLog) -> dict:
    d = {c: getattr(row, c) for c in EXPORT_COLUMNS}
    d["created_at"] = _iso(row.created_at)
    for k in ("id", "tenant_id", "actor_user_id"):
        d[k] = _s(d[k])
    return d


# ── Retention ────────────────────────────────────────────────────────────────

async def purge_expired() -> int:
    """Delete audit rows older than AUDIT_RETENTION_DAYS. Sets the session
    flag the immutability trigger checks; nothing else may delete."""
    days = settings.AUDIT_RETENTION_DAYS
    if not days or days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL nexus.audit_purge = '1'"))
        result = await db.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
        await db.commit()
        n = result.rowcount or 0
    if n:
        logger.info("audit retention: purged %d entries older than %d days", n, days)
    return n


async def retention_sweep_loop() -> None:
    """Daily retention sweep for audit entries and answer traces, started
    once from the app lifespan. First pass runs shortly after boot."""
    import asyncio

    from app.services import trace_service

    await asyncio.sleep(60)
    while True:
        try:
            await purge_expired()
        except Exception:
            logger.exception("audit retention sweep failed")
        try:
            await trace_service.purge_expired()
        except Exception:
            logger.exception("trace retention sweep failed")
        await asyncio.sleep(24 * 3600)
