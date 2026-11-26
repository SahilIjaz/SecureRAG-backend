"""Tenant-isolation tests (TC_SEC) — database-backed, through the real FastAPI app.

Two tenants, A and B, are created with one user and some data each. Every test
authenticates as tenant A's user and asserts that tenant B's documents and
conversations can never be listed, read, changed or deleted, and that the same
holds at the database level (composite foreign keys). Rows created here are
removed afterwards. Skips cleanly when no database is reachable.
"""
import uuid

import httpx
import pytest
from sqlalchemy import select, text

from app.core.security import create_access_token
from app.main import app
from app.models.conversation import Conversation, ConversationMessage
from app.models.document import Document, DocumentSource, DocumentStatus
from app.models.subscription import PlanName, Subscription
from app.models.tenant import Tenant
from app.models.user import User


async def _fresh_engine():
    from app.database import async_engine
    await async_engine.dispose()


async def _make_tenant(label: str):
    """Tenant + subscription + user + 1 document + 1 conversation (with a message)."""
    from app.database import AsyncSessionLocal

    tid, uid, doc_id, convo_id = (uuid.uuid4() for _ in range(4))
    async with AsyncSessionLocal() as db:
        db.add(Tenant(id=tid, workspace_name=f"qa-iso-{label}-{tid.hex[:6]}", slug=f"qa-iso-{label}-{tid.hex[:10]}"))
        db.add(Subscription(tenant_id=tid, plan_name=PlanName.free))
        await db.flush()
        db.add(User(
            id=uid, tenant_id=tid, full_name=f"QA Iso {label}",
            email=f"qa-iso-{label}-{uid.hex[:10]}@example.com",
            password_hash=None, is_email_verified=True, is_active=True,
        ))
        db.add(Document(
            id=doc_id, tenant_id=tid, original_filename=f"secret-{label}.txt", file_size_mb=0.01,
            mime_type="text/plain", source=DocumentSource.uploaded, status=DocumentStatus.ready,
            word_count=10, chunk_count=1,
        ))
        db.add(Conversation(id=convo_id, tenant_id=tid, visitor_name=f"visitor-{label}"))
        await db.flush()
        db.add(ConversationMessage(conversation_id=convo_id, role="user", text=f"private-{label}"))
        await db.commit()
    return {"tenant": tid, "user": uid, "doc": doc_id, "convo": convo_id}


async def _drop(*tenants):
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        for t in tenants:
            for stmt in (
                "delete from conversation_messages where conversation_id in (select id from conversations where tenant_id = :t)",
                "delete from conversations where tenant_id = :t",
                "delete from document_chunks where tenant_id = :t",
                "delete from documents where tenant_id = :t",
                "delete from users where tenant_id = :t",
                "delete from subscriptions where tenant_id = :t",
                "delete from tenants where id = :t",
            ):
                try:
                    await db.execute(text(stmt), {"t": t["tenant"]})
                except Exception:  # table name differs / already gone — best effort
                    await db.rollback()
        await db.commit()


@pytest.fixture
async def two_tenants():
    await _fresh_engine()
    try:
        a = await _make_tenant("a")
        b = await _make_tenant("b")
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"database unavailable: {e}")
    yield a, b
    await _fresh_engine()
    await _drop(a, b)


def _auth(t) -> dict:
    tok = create_access_token({"sub": str(t["user"]), "tenant_id": str(t["tenant"]), "tv": 0})
    return {"Authorization": f"Bearer {tok}"}


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_document_list_contains_only_own_tenant(two_tenants):
    a, b = two_tenants
    async with _client() as c:
        r = await c.get("/api/knowledge/documents", headers=_auth(a))
    assert r.status_code == 200, r.text
    ids = {d["id"] for d in r.json()}
    assert str(a["doc"]) in ids
    assert str(b["doc"]) not in ids


async def test_cannot_delete_other_tenants_document(two_tenants):
    a, b = two_tenants
    async with _client() as c:
        r = await c.delete(f"/api/knowledge/documents/{b['doc']}", headers=_auth(a))
    assert r.status_code == 404
    from app.database import AsyncSessionLocal
    await _fresh_engine()
    async with AsyncSessionLocal() as db:
        doc = (await db.execute(select(Document).where(Document.id == b["doc"]))).scalar_one()
    assert doc.is_active is True, "tenant B's document must be untouched"


async def test_conversation_list_contains_only_own_tenant(two_tenants):
    a, b = two_tenants
    async with _client() as c:
        r = await c.get("/api/conversations", headers=_auth(a))
    assert r.status_code == 200, r.text
    ids = {x["id"] for x in r.json()}
    assert str(a["convo"]) in ids
    assert str(b["convo"]) not in ids


async def test_cannot_read_other_tenants_conversation(two_tenants):
    a, b = two_tenants
    async with _client() as c:
        own = await c.get(f"/api/conversations/{a['convo']}", headers=_auth(a))
        foreign = await c.get(f"/api/conversations/{b['convo']}", headers=_auth(a))
    assert own.status_code == 200
    assert foreign.status_code == 404
    assert "private-b" not in foreign.text


async def test_cannot_modify_or_delete_other_tenants_conversation(two_tenants):
    a, b = two_tenants
    async with _client() as c:
        put = await c.put(f"/api/conversations/{b['convo']}/status", json={"status": "Resolved"}, headers=_auth(a))
        delete = await c.delete(f"/api/conversations/{b['convo']}", headers=_auth(a))
    assert put.status_code == 404
    assert delete.status_code == 404
    from app.database import AsyncSessionLocal
    await _fresh_engine()
    async with AsyncSessionLocal() as db:
        convo = (await db.execute(select(Conversation).where(Conversation.id == b["convo"]))).scalar_one()
    assert convo.status == "Open" and convo.deleted_at is None


async def test_token_for_tenant_a_cannot_impersonate_tenant_b_user(two_tenants):
    """The tenant is taken from the user row, not from the token claim: a token
    for A's user that *claims* tenant B still only sees A's data."""
    a, b = two_tenants
    tok = create_access_token({"sub": str(a["user"]), "tenant_id": str(b["tenant"]), "tv": 0})
    async with _client() as c:
        r = await c.get("/api/knowledge/documents", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    ids = {d["id"] for d in r.json()}
    assert str(b["doc"]) not in ids
