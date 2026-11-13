"""
Audit trail + response traceability (app/services/audit_service.py,
app/core/audit_middleware.py, app/services/trace_service.py).

Pure tests run anywhere; the DB-backed ones use the configured database
and skip themselves when it is unreachable.

Run with:  PYTHONPATH=. ./venv/bin/pytest tests/test_audit.py -v
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import settings
from app.core import audit_middleware as mw
from app.services import audit_service, trace_service
from app.services.rag_service import _build_trace


# ── Hash chain (pure) ────────────────────────────────────────────────────────

def _entry(**over):
    base = {
        "tenant_id": uuid.UUID("11111111-1111-1111-1111-111111111111"), "actor_user_id": None,
        "actor_email": "owner@example.com", "actor_role": "owner", "action": "document.delete",
        "resource_type": "document", "resource_id": "abc", "resource_label": "pricing.pdf",
        "outcome": "success", "status_code": 200, "method": "DELETE", "path": "/api/knowledge/documents/abc",
        "ip": "127.0.0.1", "user_agent": "pytest", "request_id": "r1", "details": {"chunks": 3},
        "created_at": datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc),
    }
    base.update(over)
    return base


def test_hash_is_deterministic_and_covers_content():
    h1 = audit_service.compute_hash(None, _entry())
    assert h1 == audit_service.compute_hash(None, _entry())
    assert h1 != audit_service.compute_hash(None, _entry(outcome="failure"))
    assert h1 != audit_service.compute_hash(None, _entry(actor_email="other@example.com"))
    assert h1 != audit_service.compute_hash(None, _entry(details={"chunks": 4}))
    assert h1 != audit_service.compute_hash("prev", _entry())


def test_canonical_content_is_timezone_and_type_stable():
    naive = _entry(created_at=datetime(2026, 11, 13, 12, 0))
    aware = _entry(created_at=datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc))
    assert audit_service.canonical_content(naive) == audit_service.canonical_content(aware)
    as_str = _entry(tenant_id="11111111-1111-1111-1111-111111111111")
    assert audit_service.canonical_content(as_str) == audit_service.canonical_content(_entry())


# ── Middleware rules (pure) ──────────────────────────────────────────────────

@pytest.mark.parametrize("method,path,expected", [
    ("DELETE", "/api/knowledge/documents/abc", True),
    ("PATCH", "/api/knowledge/documents/abc/authority", True),
    ("PUT", "/api/settings/workspace", True),
    ("POST", "/api/settings/members/invite", True),
    ("GET", "/api/knowledge/documents", False),           # reads are not audited
    ("POST", "/api/public/widget/message", False),        # visitor traffic → traces, not audit
    ("POST", "/api/chatbot/test-chat", False),
    ("POST", "/api/chatbot/test-chat/stream", False),
    ("POST", "/api/auth/login", False),                   # explicit in the handler
    ("POST", "/api/auth/logout", False),
    ("PUT", "/api/user/password", False),
    ("POST", "/api/presence/ping", False),
    ("POST", "/health", False),
])
def test_should_audit_rules(method, path, expected):
    assert mw.should_audit(method, path) is expected


def test_action_names_are_friendly_or_fall_back_to_route_template():
    assert mw.action_for("DELETE", "/api/knowledge/documents/{document_id}", "/api/knowledge/documents/x") == "document.delete"
    assert mw.action_for("POST", "/api/settings/members/invite", "/api/settings/members/invite") == "team.invite"
    assert mw.action_for("PUT", "/api/new/{thing_id}", "/api/new/1") == "put /api/new/{thing_id}"
    # unknown route and no template: uuids collapse so the action stays bounded
    assert mw.action_for("POST", None, "/api/x/5b4a1a0e-0f1e-4f8c-9d2a-123456789abc/go") == "post /api/x/{id}/go"


def test_actor_from_token_rejects_garbage_and_wrong_type():
    req = SimpleNamespace(headers={"authorization": "Bearer not-a-jwt"})
    assert mw._actor_from_token(req) == (None, None)
    req = SimpleNamespace(headers={})
    assert mw._actor_from_token(req) == (None, None)


def test_client_ip_prefers_forwarded_header():
    req = SimpleNamespace(headers={"x-forwarded-for": "203.0.113.9, 10.0.0.1"}, client=SimpleNamespace(host="10.0.0.1"))
    assert mw.client_ip(req) == "203.0.113.9"
    req = SimpleNamespace(headers={}, client=SimpleNamespace(host="10.0.0.2"))
    assert mw.client_ip(req) == "10.0.0.2"


# ── Trace payload (pure) ─────────────────────────────────────────────────────

def test_build_trace_records_evidence_decision_and_usage(monkeypatch):
    import time as _t
    chunks = [
        {"document_id": "d1", "chunk_id": "c1", "sequence": 0, "page": 2, "score": 0.81, "rerank_score": 0.93, "text": "Refunds within 7 days. " * 40},
        {"document_id": "d2", "chunk_id": "c7", "sequence": 3, "score": 0.55, "text": "Shipping is free."},
    ]
    metrics = {"similarity": 0.93, "coverage": 1.0, "entropy": 0.4, "retrievalConfidence": 88,
               "conflicts": [{"type": "numeric", "resolution": {"status": "resolved"}}], "conflictSummary": {"total": 1}}
    usage = SimpleNamespace(provider="gemini", model="gemini-flash", prompt_tokens=120, completion_tokens=40, total_tokens=160)
    monkeypatch.setattr(settings, "TRACE_SNIPPET_CHARS", 50)
    t = _build_trace("refund policy?", "7 days.", "answer", chunks, metrics, usage, 88, {"d1": "policy.pdf"}, _t.monotonic() - 1.0)
    assert t["decision"] == "answer" and t["confidence"] == 88 and t["latencyMs"] >= 1000
    assert t["usage"] == {"provider": "gemini", "model": "gemini-flash", "promptTokens": 120, "completionTokens": 40, "totalTokens": 160}
    assert t["retrieval"]["retrievalConfidence"] == 88 and "conflicts" not in t["retrieval"]
    assert t["conflicts"][0]["resolution"]["status"] == "resolved"
    assert [c["documentName"] for c in t["chunks"]] == ["policy.pdf", None]
    assert t["chunks"][0]["rerankScore"] == 0.93 and t["chunks"][0]["page"] == 2
    assert len(t["chunks"][0]["snippet"]) == 51 and t["chunks"][0]["snippet"].endswith("…")
    assert t["chunks"][1]["snippet"] == "Shipping is free."


def test_build_trace_tolerates_no_context():
    t = _build_trace("q", "No relevant documents found.", "no_context", [], {"similarity": 0.0, "conflicts": []}, None, 0, None, 0.0)
    assert t["chunks"] == [] and t["usage"] is None and t["decision"] == "no_context"


# ── Database-backed ──────────────────────────────────────────────────────────

async def _fresh_engine():
    from app.database import async_engine
    await async_engine.dispose()


async def _two_tenants():
    from sqlalchemy import text
    from app.database import AsyncSessionLocal
    await _fresh_engine()
    try:
        async with AsyncSessionLocal() as db:
            ids = (await db.execute(text("select id from tenants where is_active = true limit 2"))).scalars().all()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"database unavailable: {e}")
    if len(ids) < 2:
        pytest.skip("need two tenants")
    return ids


@pytest.mark.asyncio
async def test_chain_append_verify_tamper_and_immutability():
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from app.database import AsyncSessionLocal
    from app.models.audit_log import AuditLog

    t1, t2 = await _two_tenants()
    marker = f"qa-audit-{uuid.uuid4().hex[:8]}"
    ids = []
    for i in range(3):
        rid = await audit_service.record(
            action="document.delete", tenant_id=t1, actor_email=f"{marker}@example.com",
            resource_type="document", resource_id=str(i), outcome="success", status_code=200,
            details={"marker": marker, "i": i},
        )
        assert rid is not None
        ids.append(rid)
    foreign = await audit_service.record(action="document.delete", tenant_id=t2, actor_email=f"{marker}@other.com", details={"marker": marker})
    assert foreign is not None

    try:
        async with AsyncSessionLocal() as db:
            # linked: each entry's prev_hash is the previous entry's hash
            rows = (await db.execute(
                text("select id, prev_hash, entry_hash from audit_logs where id = any(:ids) order by created_at, id"),
                {"ids": ids},
            )).all()
            assert rows[1].prev_hash == rows[0].entry_hash and rows[2].prev_hash == rows[1].entry_hash
            # tenant-scoped listing never shows the other tenant's entry
            listed, _ = await audit_service.list_entries(db, t1, actor=marker, limit=10)
            assert {r.id for r in listed} == set(ids)
            listed2, _ = await audit_service.list_entries(db, t2, actor=marker, limit=10)
            assert [r.id for r in listed2] == [foreign]
            # chain verifies
            v = await audit_service.verify_chain(db, t1)
            assert v["valid"] is True and v["entries"] >= 3
            # UPDATE is rejected by the trigger
            with pytest.raises(DBAPIError):
                await db.execute(text("update audit_logs set outcome = 'failure' where id = :id"), {"id": ids[1]})
            await db.rollback()
            # plain DELETE is rejected too
            with pytest.raises(DBAPIError):
                await db.execute(text("delete from audit_logs where id = :id"), {"id": ids[1]})
            await db.rollback()
        # Tamper simulation: verify_chain must detect a changed row. We
        # cannot UPDATE (trigger), so recompute against an in-memory edit.
        async with AsyncSessionLocal() as db:
            row = (await db.execute(text("select * from audit_logs where id = :id"), {"id": ids[1]})).mappings().one()
            edited = dict(row)
            edited["outcome"] = "failure"
            assert audit_service.compute_hash(row["prev_hash"], edited) != row["entry_hash"]
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SET LOCAL nexus.audit_purge = '1'"))
            await db.execute(text("delete from audit_logs where id = any(:ids)"), {"ids": ids + [foreign]})
            await db.commit()


@pytest.mark.asyncio
async def test_retention_purge_only_removes_expired_rows(monkeypatch):
    from sqlalchemy import text
    from app.database import AsyncSessionLocal

    t1, _ = await _two_tenants()
    old_id = await audit_service.record(action="qa.old", tenant_id=t1, created_at=None) if False else None
    # insert one old and one fresh entry through the service, then back-date the old one by inserting directly
    fresh = await audit_service.record(action="qa.retention.fresh", tenant_id=t1)
    old = uuid.uuid4()
    stale_ts = datetime.now(timezone.utc) - timedelta(days=400)
    entry = {"tenant_id": t1, "action": "qa.retention.old", "outcome": "success", "created_at": stale_ts}
    async with AsyncSessionLocal() as db:
        await db.execute(text(
            "insert into audit_logs (id, tenant_id, action, outcome, created_at, entry_hash) "
            "values (:id, :t, 'qa.retention.old', 'success', :ts, :h)"
        ), {"id": old, "t": t1, "ts": stale_ts, "h": audit_service.compute_hash(None, entry)})
        await db.commit()
    try:
        monkeypatch.setattr(settings, "AUDIT_RETENTION_DAYS", 365)
        n = await audit_service.purge_expired()
        assert n >= 1
        async with AsyncSessionLocal() as db:
            remaining = (await db.execute(text("select id from audit_logs where id = any(:ids)"), {"ids": [old, fresh]})).scalars().all()
            assert remaining == [fresh]
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SET LOCAL nexus.audit_purge = '1'"))
            await db.execute(text("delete from audit_logs where id = any(:ids)"), {"ids": [old, fresh]})
            await db.commit()


@pytest.mark.asyncio
async def test_traces_are_tenant_scoped():
    from sqlalchemy import text
    from app.database import AsyncSessionLocal

    t1, t2 = await _two_tenants()
    trace = {"question": "q", "answer": "a", "decision": "answer", "confidence": 80,
             "usage": {"provider": "gemini", "model": "m", "promptTokens": 1, "completionTokens": 1, "totalTokens": 2},
             "latencyMs": 5, "retrieval": {"retrievalConfidence": 80}, "chunks": [], "conflicts": []}
    async with AsyncSessionLocal() as db:
        row = await trace_service.record_trace(db, tenant_id=t1, trace=trace, channel="test")
        await db.commit()
        tid = row.id
    try:
        async with AsyncSessionLocal() as db:
            assert (await trace_service.get_trace(db, t1, tid)) is not None
            assert (await trace_service.get_trace(db, t2, tid)) is None
            assert (await trace_service.record_trace(db, tenant_id=t1, trace=None, channel="test")) is None
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(text("delete from answer_traces where id = :id"), {"id": tid})
            await db.commit()
