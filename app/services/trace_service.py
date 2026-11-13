"""
Response traceability — persist what the RAG pipeline saw and decided for
one reply (app/models/answer_trace.py) and link it to the stored message.

rag_service attaches a `trace` dict to every result/final event (see
rag_service._build_trace); the API callers that persist the bot message
hand that dict here together with the message id. Nothing here is on the
hot path of generating the reply.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.answer_trace import AnswerTrace

logger = logging.getLogger(__name__)


def build_trace_chunks(chunks: list[dict], doc_names: Optional[dict]) -> list[dict]:
    """The evidence record: every chunk that reached the prompt, with its
    identity and scores and a capped snippet."""
    cap = settings.TRACE_SNIPPET_CHARS
    out = []
    for c in chunks or []:
        text = c.get("text") or ""
        doc_id = str(c["document_id"]) if c.get("document_id") else None
        out.append({
            "documentId": doc_id,
            "documentName": (doc_names or {}).get(doc_id) if doc_id else None,
            # Pinecone metadata hands numeric ids back as floats ("0" → 0.0).
            "chunkId": _id_str(c.get("chunk_id")),
            "sequence": _int(c.get("sequence")),
            "page": c.get("page"),
            "score": _num(c.get("score")),
            "rerankScore": _num(c.get("rerank_score")),
            "snippet": text[:cap] + ("…" if len(text) > cap else ""),
        })
    return out


def _num(v):
    try:
        return None if v is None else round(float(v), 6)
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return None if v is None else int(float(v))
    except (TypeError, ValueError):
        return None


def _id_str(v):
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


async def record_trace(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | str,
    trace: Optional[dict],
    channel: str,
    conversation_id: Optional[uuid.UUID] = None,
    message_id: Optional[uuid.UUID] = None,
    trace_id: Optional[uuid.UUID] = None,
) -> Optional[AnswerTrace]:
    """Add an AnswerTrace to `db` (caller commits). Returns the row or None
    when there is nothing to record. Never raises. `trace_id` lets a
    streaming handler announce the id to the client before the row exists."""
    if not trace:
        return None
    try:
        usage = trace.get("usage") or {}
        row = AnswerTrace(
            id=trace_id or uuid.uuid4(),
            tenant_id=uuid.UUID(str(tenant_id)),
            conversation_id=conversation_id,
            message_id=message_id,
            channel=channel[:16],
            question=(trace.get("question") or "")[:8000],
            answer=(trace.get("answer") or "")[:16000],
            decision=(trace.get("decision") or "answer")[:24],
            confidence=trace.get("confidence"),
            provider=usage.get("provider"),
            model=(usage.get("model") or None) and str(usage.get("model"))[:100],
            prompt_tokens=usage.get("promptTokens"),
            completion_tokens=usage.get("completionTokens"),
            total_tokens=usage.get("totalTokens"),
            latency_ms=trace.get("latencyMs"),
            retrieval=trace.get("retrieval"),
            chunks=trace.get("chunks"),
            conflicts=trace.get("conflicts"),
        )
        # SAVEPOINT: if the flush fails the caller's session stays usable —
        # the visitor's message must never be lost because its trace was.
        async with db.begin_nested():
            db.add(row)
            await db.flush()
        return row
    except Exception:
        logger.exception("TRACE WRITE FAILED tenant=%s channel=%s", tenant_id, channel)
        return None


async def get_trace(db: AsyncSession, tenant_id: uuid.UUID, trace_id: uuid.UUID) -> Optional[AnswerTrace]:
    return (await db.execute(
        select(AnswerTrace).where(AnswerTrace.id == trace_id, AnswerTrace.tenant_id == tenant_id)
    )).scalar_one_or_none()


async def list_traces(
    db: AsyncSession, tenant_id: uuid.UUID, *, conversation_id: Optional[uuid.UUID] = None,
    limit: int = 50, cursor: Optional[datetime] = None,
) -> tuple[list[AnswerTrace], Optional[datetime]]:
    q = select(AnswerTrace).where(AnswerTrace.tenant_id == tenant_id)
    if conversation_id:
        q = q.where(AnswerTrace.conversation_id == conversation_id)
    if cursor:
        q = q.where(AnswerTrace.created_at < cursor)
    q = q.order_by(AnswerTrace.created_at.desc()).limit(limit + 1)
    rows = list((await db.execute(q)).scalars().all())
    next_cursor = rows[limit - 1].created_at if len(rows) > limit else None
    return rows[:limit], next_cursor


async def purge_expired() -> int:
    days = settings.TRACE_RETENTION_DAYS
    if not days or days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with AsyncSessionLocal() as db:
        result = await db.execute(delete(AnswerTrace).where(AnswerTrace.created_at < cutoff))
        await db.commit()
        n = result.rowcount or 0
    if n:
        logger.info("trace retention: purged %d traces older than %d days", n, days)
    return n
