"""
Frontend-compat knowledge-base endpoints (/api/knowledge/...).

  - GET    /api/knowledge/stats           -> KnowledgeStats
  - GET    /api/knowledge/documents       -> KnowledgeDocument[]
  - POST   /api/knowledge/documents       -> KnowledgeDocument[] (multipart upload)
  - DELETE /api/knowledge/documents/{id}  -> (soft delete)
  - GET    /api/knowledge/urls            -> KnowledgeUrl[]
  - POST   /api/knowledge/urls {url}      -> {url: KnowledgeUrl}
  - POST   /api/knowledge/reindex         -> {success}

Uploaded/sample documents appear in the Documents tab; scraped websites
(source == scraped) appear in the URLs tab.
"""

import logging
import uuid
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.frontend import helpers
from app.config import settings
from app.core import vector_store
from app.core.rate_limit import limiter
from app.core.storage import delete_file_from_cloudinary
from app.database import get_db
from app.models.document import Document, DocumentSource, DocumentStatus
from app.models.user import User
from app.schemas.frontend import (
    FEAddFaqRequest,
    FEAddFaqResponse,
    FEAddUrlRequest,
    FEAddUrlResponse,
    FEDocumentAuthority,
    FEFaqEntry,
    FEKnowledgeDocument,
    FEKnowledgeStats,
    FEKnowledgeUrl,
    FESuccessResponse,
    FEUpdateDocumentAuthorityRequest,
    FEUpdateFaqRequest,
)
from app.services import document_authority_service, document_service
from app.services.auth_service import get_current_user
from app.core.subscription_guard import require_active_subscription
from app.core.rbac import require_admin
from app.services.indexing_service import schedule_document_processing, schedule_url_scrape

logger = logging.getLogger(__name__)

# Knowledge base is owner/admin territory — agents handle conversations only,
# so every knowledge route (read and write) requires an admin+ role.
router = APIRouter(
    prefix="/knowledge",
    tags=["Frontend — Knowledge"],
    dependencies=[Depends(require_admin)],
)

def _doc_label(doc: Document) -> str:
    """Human name for any knowledge source, whatever its tab."""
    if doc.source == DocumentSource.faq:
        return (doc.question or "FAQ").strip()[:80]
    if doc.source == DocumentSource.scraped:
        return doc.source_url or doc.original_filename
    return doc.original_filename

def _fe_authority(doc: Document, by_id: dict | None = None, superseded_by: dict | None = None) -> FEDocumentAuthority:
    """Authority block for one document. `by_id` ({id: Document} of the
    tenant's active docs) resolves the superseded document's name;
    `superseded_by` is document_authority_service.superseded_by_map()."""
    sup_id = str(doc.supersedes_document_id) if doc.supersedes_document_id else None
    sup_doc = (by_id or {}).get(sup_id) if sup_id else None
    newer = (superseded_by or {}).get(str(doc.id))
    return FEDocumentAuthority(
        version=int(doc.version or 1),
        effectiveDate=doc.effective_date.isoformat() if doc.effective_date else None,
        approvalStatus=doc.approval_status or "unverified",
        supersedesDocumentId=sup_id,
        supersedesDocumentName=_doc_label(sup_doc) if sup_doc else None,
        supersededByDocumentId=str(newer.id) if newer else None,
        supersededByDocumentName=_doc_label(newer) if newer else None,
        authorityRank=int(doc.authority_rank or 0),
        updatedAt=doc.authority_updated_at.isoformat() if doc.authority_updated_at else None,
    )

def _authority_context(docs: list[Document]) -> tuple[dict, dict]:
    """(by_id, superseded_by) lookups shared by every row of one listing so
    names resolve without a query per row."""
    by_id = {str(d.id): d for d in docs}
    return by_id, document_authority_service.superseded_by_map(docs)

def _fe_document(doc: Document, ctx: tuple[dict, dict] | None = None) -> FEKnowledgeDocument:
    by_id, superseded_by = ctx or ({}, {})
    return FEKnowledgeDocument(
        id=str(doc.id),
        name=doc.original_filename,
        status=helpers.doc_status_to_fe(doc.status),
        chunks=doc.chunk_count,
        sizeLabel=helpers.size_label(doc.file_size_mb),
        addedLabel=helpers.time_ago(doc.created_at),
        error=doc.error_message if doc.status == DocumentStatus.failed else None,
        authority=_fe_authority(doc, by_id, superseded_by),
    )

def _fe_url(doc: Document, ctx: tuple[dict, dict] | None = None) -> FEKnowledgeUrl:
    by_id, superseded_by = ctx or ({}, {})
    return FEKnowledgeUrl(
        id=str(doc.id),
        url=doc.source_url or doc.original_filename,
        status=helpers.doc_status_to_fe(doc.status),
        chunks=doc.chunk_count,
        addedLabel=helpers.time_ago(doc.created_at),
        error=doc.error_message if doc.status == DocumentStatus.failed else None,
        authority=_fe_authority(doc, by_id, superseded_by),
    )

def _fe_faq(doc: Document, ctx: tuple[dict, dict] | None = None) -> FEFaqEntry:
    by_id, superseded_by = ctx or ({}, {})
    return FEFaqEntry(
        id=str(doc.id),
        question=doc.question or "",
        answer=doc.answer or "",
        status=helpers.doc_status_to_fe(doc.status),
        chunks=doc.chunk_count,
        addedLabel=helpers.time_ago(doc.created_at),
        authority=_fe_authority(doc, by_id, superseded_by),
    )

@router.get("/stats", response_model=FEKnowledgeStats)
async def get_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEKnowledgeStats:
    docs = await helpers.get_active_documents(current_user.tenant_id, db)
    file_docs, url_docs, faq_docs = helpers.split_docs_and_urls(docs)

    subscription = await helpers.get_subscription(current_user.tenant_id, db)
    quota = await helpers.get_quota(current_user.tenant_id, db)
    fe_plan = helpers.fe_plan_for_subscription(subscription)
    display = helpers.PLAN_DISPLAY_LIMITS[fe_plan]

    docs_limit = display["docs"]
    if quota and quota.max_documents != -1:
        docs_limit = quota.max_documents

    return FEKnowledgeStats(
        # FAQ entries share the same document quota as files, so they count
        # toward this total too.
        documentsUsed=len(file_docs) + len(faq_docs),
        documentsLimit=docs_limit,
        urlsUsed=len(url_docs),
        urlsLimit=display["urls"],
        totalChunks=sum(d.chunk_count or 0 for d in docs),
        chunksLimit=display["chunks"],
    )

@router.get("/documents", response_model=List[FEKnowledgeDocument])
async def list_documents(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[FEKnowledgeDocument]:
    docs = await helpers.get_active_documents(current_user.tenant_id, db)
    file_docs, _url_docs, _faq_docs = helpers.split_docs_and_urls(docs)
    ctx = _authority_context(docs)
    return [_fe_document(d, ctx) for d in file_docs]

@router.post("/documents", response_model=List[FEKnowledgeDocument], status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def upload_documents(
    request: Request,
    files: List[UploadFile] = File(...),
    current_user: User = Depends(require_active_subscription),
    db: AsyncSession = Depends(get_db),
) -> List[FEKnowledgeDocument]:
    # Reject an oversized request from its Content-Length before buffering the
    # whole multipart body into memory (the per-file/quota checks happen after
    # reading otherwise). Ceiling is generous — the largest plan's per-file cap
    # plus multipart overhead — so legitimate batch uploads still pass.
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit():
        max_body_bytes = int(settings.MAX_REQUEST_BODY_MB) * 1024 * 1024
        if int(content_length) > max_body_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Upload too large. Maximum request size is {settings.MAX_REQUEST_BODY_MB}MB.",
            )
    saved = await document_service.upload_documents(current_user, files, db)
    schedule_document_processing([d.id for d in saved])
    return [_fe_document(d) for d in saved]

@router.delete("/documents/{document_id}", response_model=FESuccessResponse)
@limiter.limit("20/minute")
async def delete_document(
    request: Request,
    document_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FESuccessResponse:
    try:
        doc_id = uuid.UUID(document_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")

    result = await db.execute(
        select(Document).where(
            Document.id == doc_id,
            Document.tenant_id == current_user.tenant_id,
            Document.is_active == True,
        )
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")

    request.state.audit = {
        "resource_type": "document", "resource_id": str(doc.id), "resource_label": _doc_label(doc),
        "details": {"source": doc.source.value, "chunks": doc.chunk_count},
    }
    doc.is_active = False

    # Documents are soft-deleted (is_active=False), so the FK cascade never
    # fires — purge the keyword-search chunks explicitly, or a deleted doc's
    # text would keep matching hybrid search.
    from app.models.document_chunk import DocumentChunk
    await db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))

    usage = await helpers.get_current_usage(current_user.tenant_id, db)
    if usage is not None:
        usage.documents_count = max(0, (usage.documents_count or 0) - 1)
        usage.storage_used_mb = max(
            0.0, round((usage.storage_used_mb or 0.0) - (doc.file_size_mb or 0.0), 4)
        )

    # The soft-delete + usage decrement above is the source of truth for
    # visibility/accounting — commit it first, then best-effort hard-purge
    # the underlying storage/vectors. A transient Cloudinary/Pinecone outage
    # shouldn't block a user-facing delete; log loudly so orphans (if any)
    # stay discoverable instead of failing silently.
    await db.commit()

    if doc.file_path:
        try:
            await delete_file_from_cloudinary(doc.file_path)
        except Exception:
            logger.exception("Failed to purge Cloudinary blob for document %s", doc.id)
    try:
        await vector_store.delete_document_chunks(str(current_user.tenant_id), str(doc.id))
    except Exception:
        logger.exception("Failed to purge Pinecone vectors for document %s", doc.id)

    return FESuccessResponse()

@router.get("/urls", response_model=List[FEKnowledgeUrl])
async def list_urls(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[FEKnowledgeUrl]:
    docs = await helpers.get_active_documents(current_user.tenant_id, db)
    _file_docs, url_docs, _faq_docs = helpers.split_docs_and_urls(docs)
    ctx = _authority_context(docs)
    return [_fe_url(d, ctx) for d in url_docs]

@router.post("/urls", response_model=FEAddUrlResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def add_url(
    request: Request,
    body: FEAddUrlRequest,
    current_user: User = Depends(require_active_subscription),
    db: AsyncSession = Depends(get_db),
) -> FEAddUrlResponse:
    """
    Creates the Document row immediately (status=pending) and returns —
    the actual scrape (headless browser, up to CRAWL4AI_TIMEOUT seconds)
    and chunk/embed both run in the background via schedule_url_scrape.
    Previously the scrape ran synchronously here, so this endpoint could
    take up to ~30-60s to respond; it now responds as fast as a DB insert.
    """
    url = body.url.strip()
    if not url:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="url is required.")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    # SSRF: reject URLs that resolve to private/loopback/link-local IPs up front
    # (defence in depth — the scraper re-validates after every redirect too).
    # validate_url_safe does blocking DNS, so run it off the event loop.
    import asyncio as _asyncio
    from app.core.ip_validator import validate_url_safe

    try:
        await _asyncio.get_event_loop().run_in_executor(None, validate_url_safe, url)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"This URL can't be added: {e}",
        )

    # Files are deduped by content_hash; URLs were not, so the same page could
    # be added repeatedly — each copy consuming document quota and feeding
    # duplicate chunks to the LLM.
    dup = await db.execute(
        select(Document.id).where(
            Document.tenant_id == current_user.tenant_id,
            Document.is_active == True,
            Document.source == DocumentSource.scraped,
            Document.source_url == url,
        ).limit(1)
    )
    if dup.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This URL is already in your knowledge base.",
        )

    quota = await document_service.lock_quota_row(current_user.tenant_id, db)
    if quota and quota.max_documents != -1:
        result = await db.execute(
            select(func.count()).select_from(Document).where(
                Document.tenant_id == current_user.tenant_id,
                Document.is_active == True,
            )
        )
        existing_count = result.scalar_one()
        if existing_count + 1 > quota.max_documents:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Document quota exceeded. Your plan allows {quota.max_documents} documents.",
            )

    # Per-plan URL sub-limit (in addition to the shared document quota).
    ent = await document_service._tenant_entitlements(current_user.tenant_id, db)
    if ent.max_urls != -1:
        url_count_result = await db.execute(
            select(func.count()).select_from(Document).where(
                Document.tenant_id == current_user.tenant_id,
                Document.is_active == True,
                Document.source == DocumentSource.scraped,
            )
        )
        if url_count_result.scalar_one() + 1 > ent.max_urls:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"URL limit exceeded. Your plan allows {ent.max_urls} URLs.",
            )

    doc = Document(
        tenant_id=current_user.tenant_id,
        original_filename=url,
        file_size_mb=0.0,
        mime_type="application/pdf",
        source=DocumentSource.scraped,
        source_url=url,
        status=DocumentStatus.pending,
    )
    db.add(doc)

    # Book the document against usage now, like every other ingestion path
    # (uploads/FAQs/samples increment at creation; DELETE decrements for all
    # sources, so skipping this here left URL docs skewing the counters).
    # Storage is booked later, in _scrape_url_into_document, once the
    # scraped size is actually known.
    usage = await helpers.get_current_usage(current_user.tenant_id, db)
    if usage is not None:
        usage.documents_count = (usage.documents_count or 0) + 1

    await db.commit()
    await db.refresh(doc)

    schedule_url_scrape([doc.id])
    return FEAddUrlResponse(url=_fe_url(doc))

@router.get("/faqs", response_model=List[FEFaqEntry])
async def list_faqs(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[FEFaqEntry]:
    docs = await helpers.get_active_documents(current_user.tenant_id, db)
    _file_docs, _url_docs, faq_docs = helpers.split_docs_and_urls(docs)
    ctx = _authority_context(docs)
    return [_fe_faq(d, ctx) for d in faq_docs]

@router.patch("/documents/{document_id}/authority", response_model=FEDocumentAuthority)
@limiter.limit("20/minute")
async def update_document_authority(
    request: Request,
    document_id: str,
    body: FEUpdateDocumentAuthorityRequest,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> FEDocumentAuthority:
    """Admin/owner-only: set a knowledge source's version, effective date,
    approval status, explicit supersession link and priority. Works for any
    source type (uploaded file, scraped URL, FAQ) — they all live in
    `documents`. The resolver in app/services/conflict_resolution.py uses
    nothing but these fields to prefer one contradicting source over another,
    so this endpoint is the single place authority can ever be granted."""
    try:
        doc_id = uuid.UUID(document_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")

    doc = await document_authority_service.update_authority(
        document_id=doc_id,
        tenant_id=current_user.tenant_id,
        actor_user_id=current_user.id,
        fields_set=set(body.model_fields_set),
        version=body.version,
        effective_date=body.effectiveDate,
        approval_status=body.approvalStatus,
        supersedes_document_id=body.supersedesDocumentId,
        authority_rank=body.authorityRank,
        db=db,
    )
    docs = await helpers.get_active_documents(current_user.tenant_id, db)
    by_id, superseded_by = _authority_context(docs)
    request.state.audit = {
        "resource_type": "document", "resource_id": str(doc.id), "resource_label": _doc_label(doc),
        "details": {k: v for k, v in body.model_dump().items() if k in body.model_fields_set},
    }
    return _fe_authority(doc, by_id, superseded_by)

@router.post("/faqs", response_model=FEAddFaqResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def add_faq(
    request: Request,
    body: FEAddFaqRequest,
    current_user: User = Depends(require_active_subscription),
    db: AsyncSession = Depends(get_db),
) -> FEAddFaqResponse:
    saved = await document_service.add_faq_entries(
        current_user, [(body.question, body.answer)], db
    )
    schedule_document_processing([d.id for d in saved])
    return FEAddFaqResponse(faq=_fe_faq(saved[0]))

@router.patch("/faqs/{faq_id}", response_model=FEFaqEntry)
@limiter.limit("10/minute")
async def update_faq(
    request: Request,
    faq_id: str,
    body: FEUpdateFaqRequest,
    current_user: User = Depends(require_active_subscription),
    db: AsyncSession = Depends(get_db),
) -> FEFaqEntry:
    try:
        parsed_id = uuid.UUID(faq_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="FAQ entry not found.")

    doc = await document_service.update_faq_entry(
        current_user, parsed_id, body.question, body.answer, db
    )
    schedule_document_processing([doc.id])
    return _fe_faq(doc)

@router.post("/reindex", response_model=FESuccessResponse)
@limiter.limit("3/minute")
async def reindex_all(
    request: Request,
    current_user: User = Depends(require_active_subscription),
    db: AsyncSession = Depends(get_db),
) -> FESuccessResponse:
    """
    Re-run the indexing pipeline over documents that need it — i.e. not
    already `ready` and not already `processing` (a doc mid-index shouldn't
    be rescheduled on top of itself; that duplicates embedding calls and
    races the two runs' status updates against each other).
    """
    def _reindexable_source(d: Document) -> bool:
        # FAQ docs have no file_url (nothing to download) but are still a
        # valid indexing target — without this, "Re-index all" would
        # silently skip every FAQ entry.
        return bool(d.file_url) or d.source == DocumentSource.faq

    docs = await helpers.get_active_documents(current_user.tenant_id, db)
    reindexable = [
        d.id for d in docs
        if _reindexable_source(d) and d.status in (DocumentStatus.pending, DocumentStatus.failed)
    ]
    for doc in docs:
        if doc.status == DocumentStatus.failed and _reindexable_source(doc):
            doc.status = DocumentStatus.pending
            doc.error_message = None
    await db.commit()
    schedule_document_processing(reindexable)
    return FESuccessResponse()
