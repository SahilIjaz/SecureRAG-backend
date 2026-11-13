"""
Audit trail: append-only, hash-chained, tenant-scoped.

    record(...)        — append one entry in its own transaction. Never
                         raises to the caller: an audit failure is logged
                         loudly but must not fail the user's request.
    list_entries(...)  — cursor-paginated, filterable read for the admin UI.
    verify_chain(...)  — recompute the per-tenant hash chain and report the
                         first broken link, if any.
    export_rows(...)   — flat rows for CSV/JSON export.
    purge_expired()    — retention sweep; the only code path permitted to
                         delete (the DB trigger checks `nexus.audit_purge`).

Hash chain: entry_hash = SHA-256(prev_hash || canonical JSON of the entry's
content). prev_hash is the entry_hash of the previous entry of the same
tenant (null for the first). Appends take a per-tenant advisory lock for
the duration of the insert transaction and assign a per-tenant `seq`, so
concurrent requests can neither fork the chain nor be mis-ordered by clock
skew; reads and verification order by `seq`.

Retention: after the sweep removes the oldest rows, the oldest *surviving*
row still carries the hash of a deleted predecessor. verify_chain treats
that row as the chain's anchor (its own hash is recomputed from its stored
prev_hash) and checks linkage from the next row on.

Canonicalisation: `details` is normalised to plain JSON (dates/UUIDs →
strings, numbers → their decimal form) BEFORE hashing and storing, so what
Postgres hands back from JSONB hashes identically to what was hashed.

Threat model: protects against accidental or application-level edits and
ORM deletes; a compromised database credential is out of scope (it could
drop the trigger). Pair with database-level backups/WAL for that.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.audit_log import AuditLog

logger = logging.getLogger(__name__)

GLOBAL_CHAIN_KEY = "global"  # events with no tenant (unknown-email logins)
EXPORT_ROW_LIMIT = 100_000


# ── Canonicalisation + hashing (pure) ────────────────────────────────────────

def _canon_number(v) -> str:
    """Decimal text that is identical for a Python float/int and for the
    numeric Postgres JSONB returns for it (1e22 → '10000000000000000000000',
    -0.0 → '0', 1.5 → '1.5')."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            return repr(v)
        d = Decimal(repr(v))
    else:
        d = Decimal(str(v))
    if d == 0:
        return "0"
    s = format(d.normalize(), "f")
    return s


def _canon(v):
    """Recursively normalise a JSON-ish value for hashing."""
    if v is None or isinstance(v, (str, bool)):
        return v
    if isinstance(v, (int, float, Decimal)):
        return {"$n": _canon_number(v)}
    if isinstance(v, dict):
        return {str(k): _canon(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    return str(v)


def normalize_details(details: Optional[dict]) -> Optional[dict]:
    """What gets STORED: plain JSON (dates/UUIDs/Decimals → strings via
    default=str, non-finite floats → strings) so the insert can never fail
    on a value type and the read-back is JSON-equal."""
    if details is None:
        return None

    def fix(v):
        if isinstance(v, float) and not math.isfinite(v):
            return repr(v)
        if isinstance(v, dict):
            return {str(k): fix(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [fix(x) for x in v]
        return v

    try:
        return json.loads(json.dumps(fix(details), default=str))
    except (TypeError, ValueError):
        return {"_unserialisable": str(details)[:500]}


HASH_VERSION = 2  # scheme new rows are written with; verify accepts every known version
KNOWN_HASH_VERSIONS = (2, 1)


def canonical_content(entry: dict, version: int = HASH_VERSION) -> str:
    """Deterministic serialisation of the hashed fields. Datetimes/UUIDs are
    rendered as strings so the Python object and the DB row hash
    identically. Version 2 passes `details` through _canon so JSONB numeric
    normalisation can't change the hash; version 1 (rows written before
    that fix) serialised the raw dict. The scheme is versioned per row so a
    future change never invalidates history."""
    details = _canon(entry.get("details")) if version >= 2 else entry.get("details")
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
        "details": details,
        "created_at": _iso(entry.get("created_at")),
    }
    if version >= 2:
        return json.dumps(covered, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return json.dumps(covered, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(prev_hash: Optional[str], entry: dict, version: int = HASH_VERSION) -> str:
    return hashlib.sha256(((prev_hash or "") + "|" + canonical_content(entry, version)).encode("utf-8")).hexdigest()


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


def _tenant_filter(tenant_id: Optional[uuid.UUID]):
    return AuditLog.tenant_id == tenant_id if tenant_id else AuditLog.tenant_id.is_(None)


# ── Writes ───────────────────────────────────────────────────────────────────

async def _append(db: AsyncSession, entry: dict) -> AuditLog:
    tenant_id = entry.get("tenant_id")
    # Per-tenant advisory lock (transaction-scoped) → the "previous row" read
    # and the seq assignment below are stable until our insert commits.
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": "audit:" + _chain_key(tenant_id)})
    prev = (await db.execute(
        select(AuditLog.entry_hash, AuditLog.seq)
        .where(_tenant_filter(tenant_id))
        .order_by(AuditLog.seq.desc().nulls_last(), AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(1)
    )).first()
    prev_hash = prev.entry_hash if prev else None
    seq = (prev.seq or 0) + 1 if prev else 1
    entry = {**entry, "created_at": entry.get("created_at") or datetime.now(timezone.utc)}
    row = AuditLog(**entry, seq=seq, prev_hash=prev_hash, hash_version=HASH_VERSION,
                   entry_hash=compute_hash(prev_hash, entry, HASH_VERSION))
    db.add(row)
    return row


async def _lookup_actor(db: AsyncSession, user_id: Optional[str], tenant_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(email, role) for a token's actor — same session as the append."""
    if not user_id:
        return None, None
    from app.models.tenant_user import TenantUser
    from app.models.user import User

    uid = _uuid(user_id)
    tid = _uuid(tenant_id)
    if uid is None:
        return None, None
    email = (await db.execute(select(User.email).where(User.id == uid))).scalar_one_or_none()
    role = None
    if tid is not None:
        role = (await db.execute(
            select(TenantUser.role).where(TenantUser.tenant_id == tid, TenantUser.user_id == uid)
        )).scalar_one_or_none()
    role_s = role.value if hasattr(role, "value") else role
    return email, role_s or ("owner" if email else None)


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
    resolve_actor: bool = False,
) -> Optional[uuid.UUID]:
    """Append one audit entry in its own transaction. With `resolve_actor`,
    the actor's email/role are looked up (one session for everything).
    Returns the new row id, or None if auditing is disabled or the write
    failed (logged)."""
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
        "request_id": request_id, "details": normalize_details(details),
    }
    try:
        async with AsyncSessionLocal() as db:
            if resolve_actor and actor_user_id:
                email, role = await _lookup_actor(db, str(actor_user_id), _s(tenant_id))
                entry["actor_email"] = entry["actor_email"] or email
                entry["actor_role"] = entry["actor_role"] or role
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

def _order_desc():
    return (AuditLog.seq.desc().nulls_last(), AuditLog.created_at.desc(), AuditLog.id.desc())


async def list_entries(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    limit: int = 50,
    cursor: Optional[int] = None,
    action: Optional[str] = None,
    actor: Optional[str] = None,
    outcome: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> tuple[list[AuditLog], Optional[int]]:
    """Newest first. `cursor` is the seq of the last row seen (strictly
    less-than, so no boundary row is skipped or repeated)."""
    q = select(AuditLog).where(AuditLog.tenant_id == tenant_id)
    if cursor is not None:
        q = q.where(AuditLog.seq < cursor)
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
    q = q.order_by(*_order_desc()).limit(limit + 1)
    rows = list((await db.execute(q)).scalars().all())
    next_cursor = rows[limit - 1].seq if len(rows) > limit else None
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


def verify_rows(rows: list[AuditLog]) -> dict:
    """Pure chain check over rows in chain order (oldest first). The first
    row is the anchor: its predecessor may have been purged, so only its own
    content is checked against its stored prev_hash."""
    prev: Optional[str] = None
    now = datetime.now(timezone.utc)
    for i, row in enumerate(rows):
        if i > 0 and row.prev_hash != prev:
            return {"valid": False, "entries": len(rows), "checked": i, "brokenAt": str(row.id),
                    "reason": "previous-hash link does not match", "checkedAt": now}
        content = _row_dict(row)
        versions = [row.hash_version or 1] + [v for v in KNOWN_HASH_VERSIONS if v != (row.hash_version or 1)]
        if not any(row.entry_hash == compute_hash(row.prev_hash, content, v) for v in versions):
            return {"valid": False, "entries": len(rows), "checked": i, "brokenAt": str(row.id),
                    "reason": "entry content does not match its hash", "checkedAt": now}
        prev = row.entry_hash
    return {"valid": True, "entries": len(rows), "checked": len(rows), "brokenAt": None, "reason": None, "checkedAt": now}


async def verify_chain(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    rows = (await db.execute(
        select(AuditLog).where(AuditLog.tenant_id == tenant_id)
        .order_by(AuditLog.seq.asc().nulls_first(), AuditLog.created_at.asc(), AuditLog.id.asc())
    )).scalars().all()
    return verify_rows(list(rows))


async def export_rows(
    db: AsyncSession, tenant_id: uuid.UUID, *, since: Optional[datetime] = None, until: Optional[datetime] = None,
    limit: int = EXPORT_ROW_LIMIT,
) -> Iterable[AuditLog]:
    q = select(AuditLog).where(AuditLog.tenant_id == tenant_id)
    if since:
        q = q.where(AuditLog.created_at >= since)
    if until:
        q = q.where(AuditLog.created_at <= until)
    q = q.order_by(AuditLog.seq.asc().nulls_first(), AuditLog.created_at.asc(), AuditLog.id.asc()).limit(limit)
    return (await db.execute(q)).scalars().all()


EXPORT_COLUMNS = [
    "id", "seq", "created_at", "tenant_id", "actor_user_id", "actor_email", "actor_role", "action",
    "resource_type", "resource_id", "resource_label", "outcome", "status_code", "method", "path",
    "ip", "user_agent", "request_id", "details", "prev_hash", "entry_hash", "hash_version",
]


def export_dict(row: AuditLog) -> dict:
    d = {c: getattr(row, c) for c in EXPORT_COLUMNS}
    d["created_at"] = _iso(row.created_at)
    for k in ("id", "tenant_id", "actor_user_id"):
        d[k] = _s(d[k])
    return d


# ── Retention ────────────────────────────────────────────────────────────────

async def purge_expired(tenant_id: Optional[uuid.UUID] = None) -> int:
    """Delete audit rows older than AUDIT_RETENTION_DAYS (optionally for one
    tenant). Sets the session flag the immutability trigger checks; nothing
    else may delete. The oldest surviving row becomes the chain anchor."""
    days = settings.AUDIT_RETENTION_DAYS
    if not days or days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL nexus.audit_purge = '1'"))
        q = delete(AuditLog).where(AuditLog.created_at < cutoff)
        if tenant_id is not None:
            q = q.where(AuditLog.tenant_id == tenant_id)
        result = await db.execute(q)
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
