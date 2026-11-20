"""
Storage/knowledge-size limits (app/core/entitlements.py max_words,
app/services/indexing_service.py._enforce_knowledge_limits,
app/services/document_service.py.add_faq_entries storage check,
app/api/frontend/helpers.py.get_active_document_totals).

DB-backed: creates a throwaway tenant + subscription + documents on the
configured database and hard-deletes the tenant at the end (cascade removes
everything else). Skips itself when the database is unreachable.

Run with:  PYTHONPATH=. ./venv/bin/pytest tests/test_storage_limits.py -v
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from app.core.entitlements import PLAN_ENTITLEMENTS
from app.models.document import Document, DocumentSource, DocumentStatus
from app.models.subscription import PlanName, Subscription
from app.models.tenant import Tenant


# ── Pure: plan table itself ──────────────────────────────────────────────────

def test_max_words_is_defined_and_increasing_by_plan():
    starter = PLAN_ENTITLEMENTS[PlanName.free].max_words
    growth = PLAN_ENTITLEMENTS[PlanName.pro].max_words
    business = PLAN_ENTITLEMENTS[PlanName.pro_plus].max_words
    assert starter == 700_000
    assert growth == 4_000_000
    assert business == 27_000_000
    assert starter < growth < business


# ── DB-backed fixtures ───────────────────────────────────────────────────────

async def _fresh_engine():
    from app.database import async_engine
    await async_engine.dispose()


async def _make_tenant(plan: PlanName = PlanName.free) -> uuid.UUID:
    from app.database import AsyncSessionLocal

    await _fresh_engine()
    tenant_id = uuid.uuid4()
    try:
        async with AsyncSessionLocal() as db:
            db.add(Tenant(id=tenant_id, workspace_name=f"qa-storage-{tenant_id.hex[:6]}", slug=f"qa-storage-{tenant_id.hex[:10]}"))
            db.add(Subscription(tenant_id=tenant_id, plan_name=plan))
            await db.commit()
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"database unavailable: {e}")
    return tenant_id


async def _add_document(tenant_id: uuid.UUID, *, words: int, chunks: int, mb: float = 0.01) -> None:
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        db.add(Document(
            tenant_id=tenant_id, original_filename="seed.txt", file_url="https://example.com/x",
            file_size_mb=mb, mime_type="text/plain", source=DocumentSource.uploaded,
            status=DocumentStatus.ready, word_count=words, chunk_count=chunks,
        ))
        await db.commit()


async def _drop_tenant(tenant_id: uuid.UUID) -> None:
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from tenants where id = :id"), {"id": tenant_id})
        await db.commit()


# ── _enforce_knowledge_limits ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_word_limit_blocks_a_document_that_would_exceed_it():
    from app.services.indexing_service import _enforce_knowledge_limits

    tenant_id = await _make_tenant(PlanName.free)  # max_words=700_000
    try:
        await _add_document(tenant_id, words=699_000, chunks=10)
        with pytest.raises(ValueError, match="word"):
            await _enforce_knowledge_limits(str(tenant_id), uuid.uuid4(), new_words=2_000, new_chunks=1)
    finally:
        await _drop_tenant(tenant_id)


@pytest.mark.asyncio
async def test_word_limit_allows_a_document_that_fits():
    from app.services.indexing_service import _enforce_knowledge_limits

    tenant_id = await _make_tenant(PlanName.free)
    try:
        await _add_document(tenant_id, words=699_000, chunks=10)
        # Should not raise.
        await _enforce_knowledge_limits(str(tenant_id), uuid.uuid4(), new_words=500, new_chunks=1)
    finally:
        await _drop_tenant(tenant_id)


@pytest.mark.asyncio
async def test_chunk_limit_blocks_a_document_that_would_exceed_it():
    from app.services.indexing_service import _enforce_knowledge_limits

    tenant_id = await _make_tenant(PlanName.free)  # max_chunks=2000
    try:
        await _add_document(tenant_id, words=100, chunks=1995)
        with pytest.raises(ValueError, match="chunk"):
            await _enforce_knowledge_limits(str(tenant_id), uuid.uuid4(), new_words=10, new_chunks=10)
    finally:
        await _drop_tenant(tenant_id)


@pytest.mark.asyncio
async def test_limits_check_excludes_the_document_being_indexed_itself():
    """A document that has already been indexed once (re-index path) must
    not double-count its own prior word/chunk totals against itself."""
    from app.database import AsyncSessionLocal
    from app.services.indexing_service import _enforce_knowledge_limits

    tenant_id = await _make_tenant(PlanName.free)
    try:
        async with AsyncSessionLocal() as db:
            doc = Document(
                tenant_id=tenant_id, original_filename="reindex-me.txt", file_url="https://example.com/x",
                file_size_mb=0.01, mime_type="text/plain", source=DocumentSource.uploaded,
                status=DocumentStatus.ready, word_count=699_000, chunk_count=10,
            )
            db.add(doc)
            await db.commit()
            await db.refresh(doc)
            doc_id = doc.id
        # Re-indexing the SAME document with a slightly different word count
        # must not be blocked by its own previous totals.
        await _enforce_knowledge_limits(str(tenant_id), doc_id, new_words=699_500, new_chunks=10)
    finally:
        await _drop_tenant(tenant_id)


# ── get_active_document_totals ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_active_document_totals_sums_live_and_ignores_inactive():
    from app.api.frontend.helpers import get_active_document_totals
    from app.database import AsyncSessionLocal

    tenant_id = await _make_tenant(PlanName.free)
    try:
        await _add_document(tenant_id, words=1000, chunks=5, mb=1.0)
        await _add_document(tenant_id, words=2000, chunks=7, mb=2.0)
        async with AsyncSessionLocal() as db:
            inactive = Document(
                tenant_id=tenant_id, original_filename="deleted.txt", file_url="https://example.com/x",
                file_size_mb=50.0, mime_type="text/plain", source=DocumentSource.uploaded,
                status=DocumentStatus.ready, word_count=50_000, chunk_count=100, is_active=False,
            )
            db.add(inactive)
            await db.commit()

            storage_mb, words, chunks = await get_active_document_totals(tenant_id, db)
        assert storage_mb == pytest.approx(3.0)
        assert words == 3000
        assert chunks == 12
    finally:
        await _drop_tenant(tenant_id)


# ── add_faq_entries storage check ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_faq_entries_are_blocked_once_storage_limit_is_reached():
    from app.database import AsyncSessionLocal
    from app.models.tenant_quota import TenantQuota
    from app.models.usage_count import UsageCount
    from app.models.user import User
    from app.services.document_service import add_faq_entries

    tenant_id = await _make_tenant(PlanName.free)
    try:
        user_id = uuid.uuid4()
        async with AsyncSessionLocal() as db:
            subscription_id = (
                await db.execute(select(Subscription.id).where(Subscription.tenant_id == tenant_id))
            ).scalar_one()
            db.add(User(
                id=user_id, tenant_id=tenant_id, full_name="QA Storage", email=f"qa-storage-{user_id.hex[:10]}@example.com",
                password_hash=None, is_email_verified=True, is_active=True,
            ))
            db.add(TenantQuota(
                tenant_id=tenant_id, subscription_id=subscription_id,
                max_documents=25, max_file_size_mb=20, max_questions_per_month=500, max_storage_mb=0,
            ))
            db.add(UsageCount(tenant_id=tenant_id, period_month=date(2026, 1, 1), storage_used_mb=0.0))
            await db.commit()
            user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()

            with pytest.raises(HTTPException) as exc:
                await add_faq_entries(user, [("Do you ship internationally?", "Yes, to most countries.")], db)
            await db.rollback()
        assert exc.value.status_code == 403
        assert "Storage limit" in exc.value.detail
    finally:
        await _drop_tenant(tenant_id)
