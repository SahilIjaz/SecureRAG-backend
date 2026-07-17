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

import uuid
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.api.frontend import helpers
from app.api.frontend.onboarding import _scrape_urls_tolerant
from app.database import get_db
from app.models.document import Document, DocumentStatus
from app.models.user import User
from app.schemas.frontend import (
    FEAddUrlRequest,
    FEAddUrlResponse,
    FEKnowledgeDocument,
    FEKnowledgeStats,
    FEKnowledgeUrl,
    FESuccessResponse,
)
from app.services import document_service
from app.services.auth_service import get_current_user
from app.services.indexing_service import schedule_document_processing

router = APIRouter(prefix="/knowledge", tags=["Frontend — Knowledge"])
limiter = Limiter(key_func=get_remote_address)

def _fe_document(doc: Document) -> FEKnowledgeDocument:
    return FEKnowledgeDocument(
        id=str(doc.id),
        name=doc.original_filename,
        status=helpers.doc_status_to_fe(doc.status),
        chunks=doc.chunk_count,
        sizeLabel=helpers.size_label(doc.file_size_mb),
        addedLabel=helpers.time_ago(doc.created_at),
    )

def _fe_url(doc: Document) -> FEKnowledgeUrl:
    return FEKnowledgeUrl(
        id=str(doc.id),
        url=doc.source_url or doc.original_filename,
        status=helpers.doc_status_to_fe(doc.status),
        chunks=doc.chunk_count,
        addedLabel=helpers.time_ago(doc.created_at),
    )

@router.get("/stats", response_model=FEKnowledgeStats)
async def get_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEKnowledgeStats:
    docs = await helpers.get_active_documents(current_user.tenant_id, db)
    file_docs, url_docs = helpers.split_docs_and_urls(docs)

    subscription = await helpers.get_subscription(current_user.tenant_id, db)
    quota = await helpers.get_quota(current_user.tenant_id, db)
    fe_plan = helpers.fe_plan_for_subscription(subscription)
    display = helpers.PLAN_DISPLAY_LIMITS[fe_plan]

    docs_limit = display["docs"]
    if quota and quota.max_documents != -1:
        docs_limit = quota.max_documents

    return FEKnowledgeStats(
        documentsUsed=len(file_docs),
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
    file_docs, _ = helpers.split_docs_and_urls(docs)
    return [_fe_document(d) for d in file_docs]

@router.post("/documents", response_model=List[FEKnowledgeDocument], status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def upload_documents(
    request: Request,
    files: List[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[FEKnowledgeDocument]:
    saved = await document_service.upload_documents(current_user, files, db)
    schedule_document_processing([d.id for d in saved])
    return [_fe_document(d) for d in saved]

@router.delete("/documents/{document_id}", response_model=FESuccessResponse)
async def delete_document(
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

    doc.is_active = False
    await db.flush()
    return FESuccessResponse()

@router.get("/urls", response_model=List[FEKnowledgeUrl])
async def list_urls(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[FEKnowledgeUrl]:
    docs = await helpers.get_active_documents(current_user.tenant_id, db)
    _, url_docs = helpers.split_docs_and_urls(docs)
    return [_fe_url(d) for d in url_docs]

@router.post("/urls", response_model=FEAddUrlResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def add_url(
    request: Request,
    body: FEAddUrlRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEAddUrlResponse:
    url = body.url.strip()
    if not url:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="url is required.")

    saved = await _scrape_urls_tolerant(current_user, [url], db)
    if not saved:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Could not add URL.")
    await db.commit()
    schedule_document_processing([d.id for d in saved if d.status != DocumentStatus.failed])
    return FEAddUrlResponse(url=_fe_url(saved[0]))

@router.post("/reindex", response_model=FESuccessResponse)
async def reindex_all(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FESuccessResponse:
    """Re-run the indexing pipeline over every active document in the workspace."""
    docs = await helpers.get_active_documents(current_user.tenant_id, db)
    reindexable = [d.id for d in docs if d.file_url]
    for doc in docs:
        if doc.status == DocumentStatus.failed and doc.file_url:
            doc.status = DocumentStatus.pending
    await db.commit()
    schedule_document_processing(reindexable)
    return FESuccessResponse()
