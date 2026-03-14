"""
Document Service — handles document uploads, sample document retrieval, and sample selection.
"""

import logging
import os
import uuid
from typing import List

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.document import Document, DocumentSource, DocumentStatus
from app.models.sample_document import SampleDocument
from app.models.tenant import Tenant
from app.models.tenant_quota import TenantQuota
from app.models.usage_count import UsageCount
from app.models.user import User

logger = logging.getLogger(__name__)

ALLOWED_MIME_TYPES = [m.strip() for m in settings.ALLOWED_MIME_TYPES.split(",")]


# ---------------------------------------------------------------------------
# Get sample documents for user's business category
# ---------------------------------------------------------------------------

async def get_sample_documents(user: User, db: AsyncSession) -> List[SampleDocument]:
    result = await db.execute(
        select(Tenant).where(Tenant.id == user.tenant_id)
    )
    tenant = result.scalar_one_or_none()

    if not tenant or not tenant.business_category:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Organization info not set. Complete the organization step first.",
        )

    result = await db.execute(
        select(SampleDocument).where(
            SampleDocument.business_category == tenant.business_category,
            SampleDocument.is_active == True,
        )
    )
    return result.scalars().all()


# ---------------------------------------------------------------------------
# Upload documents
# ---------------------------------------------------------------------------

async def upload_documents(
    user: User, files: List[UploadFile], db: AsyncSession
) -> List[Document]:
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No files provided.",
        )

    # Fetch tenant quota
    result = await db.execute(
        select(TenantQuota).where(TenantQuota.tenant_id == user.tenant_id)
    )
    quota = result.scalar_one_or_none()

    # Fetch current usage
    result = await db.execute(
        select(UsageCount).where(UsageCount.tenant_id == user.tenant_id)
        .order_by(UsageCount.period_month.desc())
    )
    usage = result.scalar_one_or_none()

    # Fetch existing active documents count and total storage
    result = await db.execute(
        select(Document).where(
            Document.tenant_id == user.tenant_id,
            Document.is_active == True,
        )
    )
    existing_docs = result.scalars().all()
    existing_count = len(existing_docs)
    existing_storage_mb = sum(d.file_size_mb for d in existing_docs)

    saved_documents: List[Document] = []
    total_new_storage = 0.0

    for file in files:
        # Validate MIME type
        if file.content_type not in ALLOWED_MIME_TYPES:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"File type '{file.content_type}' is not allowed. Allowed types: PDF, DOCX, DOC, TXT.",
            )

        # Read file content
        content = await file.read()
        file_size_bytes = len(content)
        file_size_mb = file_size_bytes / (1024 * 1024)

        # Validate individual file size
        if file_size_mb > settings.MAX_UPLOAD_SIZE_MB:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File '{file.filename}' exceeds maximum size of {settings.MAX_UPLOAD_SIZE_MB}MB.",
            )

        total_new_storage += file_size_mb

    # Quota checks (only if quota exists and limits are set)
    if quota:
        new_total_count = existing_count + len(files)
        new_total_storage = existing_storage_mb + total_new_storage

        if quota.max_documents != -1 and new_total_count > quota.max_documents:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Document quota exceeded. Your plan allows {quota.max_documents} documents.",
            )

        if quota.max_storage_mb != -1 and new_total_storage > quota.max_storage_mb:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Storage quota exceeded. Your plan allows {quota.max_storage_mb}MB.",
            )

    # Create tenant upload directory
    tenant_upload_dir = os.path.join(settings.UPLOAD_DIR, str(user.tenant_id))
    os.makedirs(tenant_upload_dir, exist_ok=True)

    # Save files
    for file in files:
        await file.seek(0)
        content = await file.read()
        file_size_mb = len(content) / (1024 * 1024)

        # Generate unique filename to avoid collisions
        file_ext = os.path.splitext(file.filename or "")[1]
        unique_filename = f"{uuid.uuid4()}{file_ext}"
        file_path = os.path.join(tenant_upload_dir, unique_filename)

        with open(file_path, "wb") as f:
            f.write(content)

        doc = Document(
            tenant_id=user.tenant_id,
            original_filename=file.filename or unique_filename,
            file_path=file_path,
            file_size_mb=round(file_size_mb, 4),
            mime_type=file.content_type,
            source=DocumentSource.uploaded,
            status=DocumentStatus.pending,
        )
        db.add(doc)
        saved_documents.append(doc)

    await db.flush()

    # Update usage count if exists
    if usage:
        usage.documents_count = (usage.documents_count or 0) + len(saved_documents)
        usage.storage_used_mb = round(
            (usage.storage_used_mb or 0.0) + total_new_storage, 4
        )

    await db.commit()

    for doc in saved_documents:
        await db.refresh(doc)

    logger.info(
        "Uploaded %d document(s) for tenant %s",
        len(saved_documents),
        user.tenant_id,
    )
    return saved_documents


# ---------------------------------------------------------------------------
# Select a sample document
# ---------------------------------------------------------------------------

async def select_sample_document(
    user: User, sample_document_id: uuid.UUID, db: AsyncSession
) -> Document:
    # Verify sample document exists and belongs to the right category
    result = await db.execute(
        select(SampleDocument).where(
            SampleDocument.id == sample_document_id,
            SampleDocument.is_active == True,
        )
    )
    sample_doc = result.scalar_one_or_none()

    if not sample_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sample document not found.",
        )

    # Verify sample doc matches user's business category
    result = await db.execute(
        select(Tenant).where(Tenant.id == user.tenant_id)
    )
    tenant = result.scalar_one_or_none()

    if not tenant or tenant.business_category != sample_doc.business_category:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This sample document does not match your business category.",
        )

    # Check if already selected
    result = await db.execute(
        select(Document).where(
            Document.tenant_id == user.tenant_id,
            Document.sample_document_id == sample_document_id,
            Document.is_active == True,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You have already selected this sample document.",
        )

    doc = Document(
        tenant_id=user.tenant_id,
        original_filename=sample_doc.filename,
        file_path=sample_doc.file_path,
        file_size_mb=sample_doc.file_size_mb,
        mime_type="application/pdf",
        source=DocumentSource.sample,
        status=DocumentStatus.ready,
        sample_document_id=sample_document_id,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    logger.info(
        "Tenant %s selected sample document %s",
        user.tenant_id,
        sample_document_id,
    )
    return doc


# ---------------------------------------------------------------------------
# Get all documents for a tenant
# ---------------------------------------------------------------------------

async def get_tenant_documents(user: User, db: AsyncSession) -> List[Document]:
    result = await db.execute(
        select(Document).where(
            Document.tenant_id == user.tenant_id,
            Document.is_active == True,
        ).order_by(Document.created_at.desc())
    )
    return result.scalars().all()
