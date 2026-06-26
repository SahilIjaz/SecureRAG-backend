"""
Document Service — handles document uploads, web scraping, community sample retrieval, and sample selection.
Files are stored on Cloudinary. Only the public_id and secure_url are saved in the DB.

Community samples: documents uploaded by any user are available as samples to other users
in the same business category.

Web scraping: URLs are scraped using Crawl4AI and converted to PDF before storage.
"""

import logging
import uuid
from typing import List

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.scraper import scrape_website_to_pdf
from app.core.storage import upload_file_to_cloudinary
from app.models.document import Document, DocumentSource, DocumentStatus
from app.models.tenant import Tenant
from app.models.tenant_quota import TenantQuota
from app.models.usage_count import UsageCount
from app.models.user import User

logger = logging.getLogger(__name__)

ALLOWED_MIME_TYPES = [m.strip() for m in settings.ALLOWED_MIME_TYPES.split(",")]


# ---------------------------------------------------------------------------
# Helper: attach business_category onto Document objects for the response
# ---------------------------------------------------------------------------

class DocumentWithCategory:
    """Wraps a Document and adds business_category from its owner's tenant."""
    def __init__(self, doc: Document, business_category: str) -> None:
        # Copy all Document attributes
        self.__dict__.update(doc.__dict__)
        self.business_category = business_category


# ---------------------------------------------------------------------------
# Get community sample documents for user's business category
# Returns uploaded documents from OTHER tenants in the same business category
# ---------------------------------------------------------------------------

async def get_sample_documents(user: User, db: AsyncSession) -> List[DocumentWithCategory]:
    # Get current user's tenant + business category
    result = await db.execute(
        select(Tenant).where(Tenant.id == user.tenant_id)
    )
    tenant = result.scalar_one_or_none()

    if not tenant or not tenant.business_category:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Organization info not set. Complete the organization step first.",
        )

    # Find all tenants in the same business category (excluding current user)
    result = await db.execute(
        select(Tenant).where(
            Tenant.business_category == tenant.business_category,
            Tenant.id != user.tenant_id,
        )
    )
    other_tenants = result.scalars().all()

    if not other_tenants:
        return []

    other_tenant_ids = [t.id for t in other_tenants]

    # Get uploaded (not sample) documents from those tenants that have a file_url
    result = await db.execute(
        select(Document).where(
            Document.tenant_id.in_(other_tenant_ids),
            Document.source == DocumentSource.uploaded,
            Document.is_active == True,
            Document.file_url.isnot(None),
        ).order_by(Document.created_at.desc())
    )
    docs = result.scalars().all()

    # Attach business_category to each doc
    return [DocumentWithCategory(doc, tenant.business_category) for doc in docs]


# ---------------------------------------------------------------------------
# Upload documents — stored on Cloudinary
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

    # Existing active documents
    result = await db.execute(
        select(Document).where(
            Document.tenant_id == user.tenant_id,
            Document.is_active == True,
        )
    )
    existing_docs = result.scalars().all()
    existing_count = len(existing_docs)

    # --- Validate all files first before uploading any ---
    file_data = []  # (content, file_size_mb, filename, content_type)
    total_new_storage = 0.0

    for file in files:
        if file.content_type not in ALLOWED_MIME_TYPES:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"File type '{file.content_type}' is not allowed. Allowed: PDF, DOCX, DOC, TXT.",
            )

        content = await file.read()
        file_size_mb = len(content) / (1024 * 1024)

        # Use quota's per-file limit if available, otherwise fall back to global config
        result_quota = await db.execute(
            select(TenantQuota).where(TenantQuota.tenant_id == user.tenant_id)
        )
        _quota = result_quota.scalar_one_or_none()
        max_mb = _quota.max_file_size_mb if _quota else settings.MAX_UPLOAD_SIZE_MB

        if file_size_mb > max_mb:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"'{file.filename}' exceeds the {max_mb}MB per-file limit.",
            )

        total_new_storage += file_size_mb
        file_data.append((content, file_size_mb, file.filename or "document", file.content_type))

    # --- Quota checks ---
    if quota:
        if quota.max_documents != -1 and (existing_count + len(files)) > quota.max_documents:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Document quota exceeded. Your plan allows {quota.max_documents} documents.",
            )

    # --- Upload to Cloudinary and create DB records ---
    saved_documents: List[Document] = []

    for content, file_size_mb, filename, content_type in file_data:
        public_id, secure_url = await upload_file_to_cloudinary(
            file_content=content,
            tenant_id=user.tenant_id,
            original_filename=filename,
            content_type=content_type,
        )

        doc = Document(
            tenant_id=user.tenant_id,
            original_filename=filename,
            file_path=public_id,        # Cloudinary public_id (used for deletion)
            file_url=secure_url,        # HTTPS URL to access the file
            file_size_mb=round(file_size_mb, 4),
            mime_type=content_type,
            source=DocumentSource.uploaded,
            status=DocumentStatus.pending,
        )
        db.add(doc)
        saved_documents.append(doc)

    await db.flush()

    # Update usage count
    if usage:
        usage.documents_count = (usage.documents_count or 0) + len(saved_documents)
        usage.storage_used_mb = round((usage.storage_used_mb or 0.0) + total_new_storage, 4)

    await db.commit()

    for doc in saved_documents:
        await db.refresh(doc)

    logger.info("Uploaded %d file(s) to Cloudinary for tenant %s", len(saved_documents), user.tenant_id)
    return saved_documents


# ---------------------------------------------------------------------------
# Select a community document — copies it into the current user's workspace
# ---------------------------------------------------------------------------

async def select_sample_document(
    user: User, document_id: uuid.UUID, db: AsyncSession
) -> Document:
    # Fetch the source document (must belong to a different tenant)
    result = await db.execute(
        select(Document).where(
            Document.id == document_id,
            Document.is_active == True,
            Document.file_url.isnot(None),
        )
    )
    source_doc = result.scalar_one_or_none()

    if not source_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    if source_doc.tenant_id == user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot select your own document as a sample.",
        )

    # Verify the source doc's tenant has the same business category
    result = await db.execute(
        select(Tenant).where(Tenant.id == user.tenant_id)
    )
    my_tenant = result.scalar_one_or_none()

    result = await db.execute(
        select(Tenant).where(Tenant.id == source_doc.tenant_id)
    )
    source_tenant = result.scalar_one_or_none()

    if not source_tenant or not my_tenant:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant not found.")

    if my_tenant.business_category != source_tenant.business_category:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This document is not from your business category.",
        )

    # Prevent duplicate — don't add the same source doc twice
    result = await db.execute(
        select(Document).where(
            Document.tenant_id == user.tenant_id,
            Document.file_url == source_doc.file_url,
            Document.is_active == True,
        )
    )
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You have already added this document to your workspace.",
        )

    # Create a copy in the current user's workspace (source=sample, points to same Cloudinary URL)
    doc = Document(
        tenant_id=user.tenant_id,
        original_filename=source_doc.original_filename,
        file_path=source_doc.file_path,
        file_url=source_doc.file_url,
        file_size_mb=source_doc.file_size_mb,
        mime_type=source_doc.mime_type,
        source=DocumentSource.sample,
        status=DocumentStatus.ready,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    logger.info(
        "Tenant %s selected community doc %s from tenant %s",
        user.tenant_id, document_id, source_doc.tenant_id,
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


# ---------------------------------------------------------------------------
# Select platform sample documents — copies SampleDocument records into workspace
# ---------------------------------------------------------------------------

async def select_platform_sample_documents(
    user: User, sample_document_ids: List[uuid.UUID], db: AsyncSession
) -> List[Document]:
    """
    Copies platform sample documents (SampleDocument records) into the user's workspace.
    Creates Document records for each selected SampleDocument, linking via sample_document_id.
    Used during onboarding when user selects "show me sample documents".
    """
    from app.models.sample_document import SampleDocument

    # Fetch the sample documents
    result = await db.execute(
        select(SampleDocument).where(
            SampleDocument.id.in_(sample_document_ids),
            SampleDocument.is_active == True,
        )
    )
    sample_docs = result.scalars().all()

    if not sample_docs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No sample documents found.",
        )

    # Create Document records for each sample document
    saved_documents = []
    for sample_doc in sample_docs:
        doc = Document(
            id=uuid.uuid4(),
            tenant_id=user.tenant_id,
            original_filename=sample_doc.filename,
            file_size_mb=sample_doc.file_size_mb,
            mime_type="application/pdf",
            source=DocumentSource.sample,
            status=DocumentStatus.ready,
            file_url=sample_doc.file_path,
            sample_document_id=sample_doc.id,
        )
        db.add(doc)
        saved_documents.append(doc)

    await db.commit()
    return saved_documents


# ---------------------------------------------------------------------------
# Scrape websites and add as documents
# ---------------------------------------------------------------------------

async def scrape_and_add_documents(
    user: User, urls: List[str], db: AsyncSession
) -> List[Document]:
    """
    Scrapes websites using Crawl4AI and adds them as PDF documents.
    Validates quotas before scraping.

    Args:
        user: Current user
        urls: List of website URLs to scrape
        db: Database session

    Returns:
        List of created Document records
    """
    if not settings.CRAWL4AI_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Web scraping is not enabled.",
        )

    if not urls:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No URLs provided.",
        )

    # Validate URLs
    for url in urls:
        if not url.startswith(("http://", "https://")):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid URL: {url}. Must start with http:// or https://",
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

    # Existing active documents
    result = await db.execute(
        select(Document).where(
            Document.tenant_id == user.tenant_id,
            Document.is_active == True,
        )
    )
    existing_docs = result.scalars().all()
    existing_count = len(existing_docs)

    # Check document count quota
    if quota and quota.max_documents != -1:
        if (existing_count + len(urls)) > quota.max_documents:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Document quota exceeded. Your plan allows {quota.max_documents} documents.",
            )

    saved_documents: List[Document] = []

    for url in urls:
        try:
            # Scrape website and convert to PDF
            pdf_content, page_title = await scrape_website_to_pdf(
                url, timeout=settings.CRAWL4AI_TIMEOUT
            )

            file_size_mb = len(pdf_content) / (1024 * 1024)

            # Check per-file size quota
            result_quota = await db.execute(
                select(TenantQuota).where(TenantQuota.tenant_id == user.tenant_id)
            )
            _quota = result_quota.scalar_one_or_none()
            max_mb = _quota.max_file_size_mb if _quota else settings.MAX_UPLOAD_SIZE_MB

            if file_size_mb > max_mb:
                logger.warning(f"Scraped content from {url} exceeds {max_mb}MB limit")
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"Content from '{url}' exceeds the {max_mb}MB per-file limit after scraping.",
                )

            # Upload PDF to Cloudinary
            public_id, secure_url = await upload_file_to_cloudinary(
                file_content=pdf_content,
                tenant_id=user.tenant_id,
                original_filename=f"{page_title}.pdf",
                content_type="application/pdf",
            )

            # Create document record
            doc = Document(
                tenant_id=user.tenant_id,
                original_filename=page_title,
                file_path=public_id,
                file_url=secure_url,
                file_size_mb=round(file_size_mb, 4),
                mime_type="application/pdf",
                source=DocumentSource.scraped,
                source_url=url,
                status=DocumentStatus.pending,
            )
            db.add(doc)
            saved_documents.append(doc)

            logger.info(f"Scraped and added document from {url} for tenant {user.tenant_id}")

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to scrape {url}: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to scrape {url}: {str(e)}",
            )

    await db.flush()

    # Update usage count
    if usage and saved_documents:
        total_storage = sum(d.file_size_mb for d in saved_documents)
        usage.documents_count = (usage.documents_count or 0) + len(saved_documents)
        usage.storage_used_mb = round((usage.storage_used_mb or 0.0) + total_storage, 4)

    await db.commit()

    for doc in saved_documents:
        await db.refresh(doc)

    logger.info("Scraped %d website(s) for tenant %s", len(saved_documents), user.tenant_id)
    return saved_documents
