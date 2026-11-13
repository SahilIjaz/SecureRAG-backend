"""
Audit trail + response traceability (app/services/audit_service.py,
app/core/audit_middleware.py, app/services/trace_service.py).

Pure tests run anywhere. The DB-backed ones create throwaway tenants on the
configured database and hard-delete them at the end (the trigger allows the
cascade), so no real tenant's chain is ever touched; they skip themselves
when the database is unreachable.

Run with:  PYTHONPATH=. ./venv/bin/pytest tests/test_audit.py -v
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
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


def test_details_hash_survives_jsonb_numeric_normalisation():
    """Postgres JSONB returns 1e22 as 10000000000000000000000, -0.0 as 0 and
    1.50 as 1.5; the hash must not depend on which form it sees."""
    python_side = {"big": 1e22, "neg0": -0.0, "f": 1.5, "i": 7, "nested": [{"x": 2.0}], "flag": True, "s": "ok"}
    db_side = {"big": 10000000000000000000000, "neg0": 0, "f": 1.5, "i": 7, "nested": [{"x": 2}], "flag": True, "s": "ok"}
    assert audit_service.compute_hash(None, _entry(details=python_side)) == audit_service.compute_hash(None, _entry(details=db_side))
    # booleans stay distinct from ints, strings from numbers
    assert audit_service.compute_hash(None, _entry(details={"v": True})) != audit_service.compute_hash(None, _entry(details={"v": 1}))
    assert audit_service.compute_hash(None, _entry(details={"v": "1"})) != audit_service.compute_hash(None, _entry(details={"v": 1}))


def test_legacy_v1_rows_still_verify_and_new_rows_use_v2():
    e = _entry(details={"chunks": 1, "source": "uploaded"})
    v1 = audit_service.compute_hash(None, e, 1)
    v2 = audit_service.compute_hash(None, e, 2)
    assert v1 != v2 and audit_service.compute_hash(None, e) == v2
    row1 = SimpleNamespace(**e, id=uuid.uuid4(), prev_hash=None, entry_hash=v1, hash_version=1)
    row2 = SimpleNamespace(**e, id=uuid.uuid4(), prev_hash=v1, entry_hash=audit_service.compute_hash(v1, e, 2), hash_version=2)
    assert audit_service.verify_rows([row1, row2])["valid"] is True
    bad = SimpleNamespace(**e, id=uuid.uuid4(), prev_hash=None, entry_hash="0" * 64, hash_version=1)
    assert audit_service.verify_rows([bad])["valid"] is False


def test_normalize_details_never_breaks_the_insert():
    n = audit_service.normalize_details({"when": date(2026, 3, 1), "id": uuid.UUID(int=5), "nan": float("nan"), "ok": 1})
    assert n == {"when": "2026-03-01", "id": "00000000-0000-0000-0000-000000000005", "nan": "nan", "ok": 1}
    assert audit_service.normalize_details(None) is None


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
    assert mw.action_for("POST", None, "/api/x/5b4a1a0e-0f1e-4f8c-9d2a-123456789abc/go") == "post /api/x/{id}/go"


def test_actor_from_token_rejects_garbage_and_wrong_type():
    assert mw._actor_from_token(SimpleNamespace(headers={"authorization": "Bearer not-a-jwt"})) == (None, None)
    assert mw._actor_from_token(SimpleNamespace(headers={})) == (None, None)


def test_client_ip_prefers_forwarded_header():
    req = SimpleNamespace(headers={"x-forwarded-for": "203.0.113.9, 10.0.0.1"}, client=SimpleNamespace(host="10.0.0.1"))
    assert mw.client_ip(req) == "203.0.113.9"
    assert mw.client_ip(SimpleNamespace(headers={}, client=SimpleNamespace(host="10.0.0.2"))) == "10.0.0.2"


def test_unmatched_or_anonymous_requests_are_not_recorded(monkeypatch):
    scheduled = []
    monkeypatch.setattr("app.core.background.fire_and_forget", lambda coro: (scheduled.append(coro), coro.close()))
    # unmatched route (no endpoint)
    req = SimpleNamespace(scope={}, headers={}, state=SimpleNamespace(), path_params={}, method="POST",
                          url=SimpleNamespace(path="/api/nope"), client=None, app=SimpleNamespace(routes=[]))
    mw._schedule_record(req, "r", 404, None)
    # matched route, no token, no enrichment
    req.scope = {"endpoint": object()}
    mw._schedule_record(req, "r", 401, None)
    assert scheduled == []
    # matched route + handler enrichment → recorded even without a token
    req.state = SimpleNamespace(audit={"action": "x.y", "tenant_id": None})
    mw._schedule_record(req, "r", 200, None)
    assert len(scheduled) == 1


# ── Trace payload (pure) ─────────────────────────────────────────────────────

def test_build_trace_records_evidence_decision_and_usage(monkeypatch):
    import time as _t
    chunks = [
        {"document_id": "d1", "chunk_id": 0.0, "sequence": 0.0, "page": 2, "score": 0.81, "rerank_score": 0.93, "text": "Refunds within 7 days. " * 40},
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
    assert t["chunks"][0]["chunkId"] == "0" and t["chunks"][0]["sequence"] == 0  # Pinecone float ids normalised
    assert t["chunks"][0]["rerankScore"] == 0.93 and t["chunks"][0]["page"] == 2
    assert len(t["chunks"][0]["snippet"]) == 51 and t["chunks"][0]["snippet"].endswith("…")


def test_build_trace_tolerates_no_context():
    t = _build_trace("q", "No relevant documents found.", "no_context", [], {"similarity": 0.0, "conflicts": []}, None, 0, None, 0.0)
    assert t["chunks"] == [] and t["usage"] is None and t["decision"] == "no_context"


# ── Database-backed (throwaway tenants) ──────────────────────────────────────

async def _fresh_engine():
    from app.database import async_engine
    await async_engine.dispose()


async def _make_tenants(n: int) -> list[uuid.UUID]:
    """Create n throwaway tenants; caller deletes them (cascade removes
    their audit rows and traces — the trigger permits that)."""
    from sqlalchemy import text
    from app.database import AsyncSessionLocal
    await _fresh_engine()
    ids = [uuid.uuid4() for _ in range(n)]
    try:
        async with AsyncSessionLocal() as db:
            for tid in ids:
                await db.execute(text("insert into tenants (id, workspace_name, slug) values (:id, :n, :s)"),
                                 {"id": tid, "n": f"qa-audit-{tid.hex[:6]}", "s": f"qa-audit-{tid.hex[:10]}"})
            await db.commit()
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"database unavailable: {e}")
    return ids


async def _drop_tenants(ids: list[uuid.UUID]) -> None:
    from sqlalchemy import text
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from tenants where id = any(:ids)"), {"ids": ids})
        await db.commit()


@pytest.mark.asyncio
async def test_chain_append_verify_tamper_immutability_and_anchor():
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from app.database import AsyncSessionLocal

    t1, t2 = await _make_tenants(2)
    try:
        ids = []
        for i in range(3):
            rid = await audit_service.record(
                action="document.delete", tenant_id=t1, actor_email="owner@example.com",
                resource_type="document", resource_id=str(i), outcome="success", status_code=200,
                details={"i": i, "ratio": 1e22, "zero": -0.0, "when": date(2026, 1, 1)},
            )
            assert rid is not None
            ids.append(rid)
        foreign = await audit_service.record(action="document.delete", tenant_id=t2, actor_email="x@other.com")
        assert foreign is not None

        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                text("select id, seq, prev_hash, entry_hash from audit_logs where tenant_id = :t order by seq"), {"t": t1},
            )).all()
            assert [r.seq for r in rows] == [1, 2, 3]
            assert rows[1].prev_hash == rows[0].entry_hash and rows[2].prev_hash == rows[1].entry_hash
            # tenant-scoped listing + seq cursor pagination
            page1, cur = await audit_service.list_entries(db, t1, limit=2)
            assert [r.seq for r in page1] == [3, 2] and cur == 2
            page2, cur2 = await audit_service.list_entries(db, t1, limit=2, cursor=cur)
            assert [r.seq for r in page2] == [1] and cur2 is None
            assert [r.id for r in (await audit_service.list_entries(db, t2, limit=10))[0]] == [foreign]
            # details round-tripped through JSONB still verify (1e22, -0.0, date)
            v = await audit_service.verify_chain(db, t1)
            assert v["valid"] is True and v["entries"] == 3, v
            # UPDATE / plain DELETE rejected by the trigger
            with pytest.raises(DBAPIError):
                await db.execute(text("update audit_logs set outcome = 'failure' where id = :id"), {"id": ids[1]})
            await db.rollback()
            with pytest.raises(DBAPIError):
                await db.execute(text("delete from audit_logs where id = :id"), {"id": ids[1]})
            await db.rollback()
            # tamper detection on an in-memory edit
            row = (await db.execute(text("select * from audit_logs where id = :id"), {"id": ids[1]})).mappings().one()
            edited = dict(row); edited["outcome"] = "failure"
            assert audit_service.compute_hash(row["prev_hash"], edited) != row["entry_hash"]

        # Retention anchor: purge the oldest row (back-date it first while
        # the trigger forbids UPDATE → re-insert a copy with an old date).
        async with AsyncSessionLocal() as db:
            await db.execute(text("SET LOCAL nexus.audit_purge = '1'"))
            await db.execute(text("delete from audit_logs where id = :id"), {"id": ids[0]})
            await db.commit()
        async with AsyncSessionLocal() as db:
            v = await audit_service.verify_chain(db, t1)
            assert v["valid"] is True and v["entries"] == 2, v  # oldest survivor is the anchor
    finally:
        await _drop_tenants([t1, t2])


@pytest.mark.asyncio
async def test_retention_purge_only_removes_expired_rows_of_that_tenant(monkeypatch):
    from sqlalchemy import text
    from app.database import AsyncSessionLocal

    (t1,) = await _make_tenants(1)
    try:
        fresh = await audit_service.record(action="qa.retention.fresh", tenant_id=t1)
        old = uuid.uuid4()
        stale_ts = datetime.now(timezone.utc) - timedelta(days=400)
        entry = {"tenant_id": t1, "action": "qa.retention.old", "outcome": "success", "created_at": stale_ts}
        async with AsyncSessionLocal() as db:
            await db.execute(text(
                "insert into audit_logs (id, tenant_id, action, outcome, created_at, seq, entry_hash) "
                "values (:id, :t, 'qa.retention.old', 'success', :ts, 0, :h)"
            ), {"id": old, "t": t1, "ts": stale_ts, "h": audit_service.compute_hash(None, entry)})
            await db.commit()
        monkeypatch.setattr(settings, "AUDIT_RETENTION_DAYS", 365)
        assert await audit_service.purge_expired(tenant_id=t1) == 1
        async with AsyncSessionLocal() as db:
            remaining = (await db.execute(text("select id from audit_logs where tenant_id = :t"), {"t": t1})).scalars().all()
            assert remaining == [fresh]
    finally:
        await _drop_tenants([t1])


@pytest.mark.asyncio
async def test_traces_are_tenant_scoped_and_a_failed_trace_does_not_poison_the_session():
    from sqlalchemy import text
    from app.database import AsyncSessionLocal

    t1, t2 = await _make_tenants(2)
    trace = {"question": "q", "answer": "a", "decision": "answer", "confidence": 80,
             "usage": {"provider": "gemini", "model": "m", "promptTokens": 1, "completionTokens": 1, "totalTokens": 2},
             "latencyMs": 5, "retrieval": {"retrievalConfidence": 80}, "chunks": [], "conflicts": []}
    try:
        async with AsyncSessionLocal() as db:
            row = await trace_service.record_trace(db, tenant_id=t1, trace=trace, channel="test")
            assert row is not None
            # a trace that cannot be stored (channel too long for the column
            # is caught by slicing, so force a real DB error via a bad uuid)
            bad = await trace_service.record_trace(db, tenant_id=t1, trace=trace, channel="test", conversation_id=uuid.uuid4())
            assert bad is None  # FK violation → None …
            await db.execute(text("select 1"))  # … and the session is still usable
            await db.commit()
            tid = row.id
        async with AsyncSessionLocal() as db:
            assert (await trace_service.get_trace(db, t1, tid)) is not None
            assert (await trace_service.get_trace(db, t2, tid)) is None
            assert (await trace_service.record_trace(db, tenant_id=t1, trace=None, channel="test")) is None
    finally:
        await _drop_tenants([t1, t2])
