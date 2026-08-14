"""
Document Service — handles document uploads, web scraping, community sample retrieval, and sample selection.
Files are stored on Cloudinary. Only the public_id and secure_url are saved in the DB.

Community samples: documents uploaded by any user are available as samples to other users
in the same business category.

Web scraping: URLs are scraped using Crawl4AI and converted to PDF before storage.
"""

import hashlib
import logging
import uuid
from typing import List, Optional

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import vector_store
from app.core.scraper import scrape_website_to_pdf
from app.core.storage import delete_file_from_cloudinary, upload_file_to_cloudinary
from app.core.validation import safe_filename, sniff_mime_or_raise
from app.models.document import Document, DocumentSource, DocumentStatus
from app.models.tenant import Tenant
from app.models.tenant_quota import TenantQuota
from app.models.usage_count import UsageCount
from app.models.user import User

logger = logging.getLogger(__name__)

ALLOWED_MIME_TYPES = [m.strip() for m in settings.ALLOWED_MIME_TYPES.split(",")]

async def lock_quota_row(tenant_id: uuid.UUID, db: AsyncSession) -> Optional[TenantQuota]:
    """
    SELECT ... FOR UPDATE on the tenant's quota row. Must be the first DB
    operation in any function that reads a document/URL count and then
    decides whether a new batch fits under quota — otherwise two concurrent
    requests can both read the same pre-write count, both pass the check,
    and both commit, exceeding the limit (TOCTOU).
    """
    result = await db.execute(
        select(TenantQuota).where(TenantQuota.tenant_id == tenant_id).with_for_update()
    )
    return result.scalar_one_or_none()

class DocumentWithCategory:
    """Wraps a Document and adds business_category from its owner's tenant."""
    def __init__(self, doc: Document, business_category: str) -> None:
        self.__dict__.update(doc.__dict__)
        self.business_category = business_category

async def get_sample_documents(user: User, db: AsyncSession) -> List[DocumentWithCategory]:
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
        select(Tenant).where(
            Tenant.business_category == tenant.business_category,
            Tenant.id != user.tenant_id,
        )
    )
    other_tenants = result.scalars().all()

    if not other_tenants:
        return []

    other_tenant_ids = [t.id for t in other_tenants]

    result = await db.execute(
        select(Document).where(
            Document.tenant_id.in_(other_tenant_ids),
            Document.source == DocumentSource.uploaded,
            Document.is_active == True,
            Document.file_url.isnot(None),
        ).order_by(Document.created_at.desc())
    )
    docs = result.scalars().all()

    return [DocumentWithCategory(doc, tenant.business_category) for doc in docs]

async def upload_documents(
    user: User, files: List[UploadFile], db: AsyncSession
) -> List[Document]:
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No files provided.",
        )

    quota = await lock_quota_row(user.tenant_id, db)

    result = await db.execute(
        select(UsageCount).where(UsageCount.tenant_id == user.tenant_id)
        .order_by(UsageCount.period_month.desc())
    )
    usage = result.scalar_one_or_none()

    # Only content_hash is needed below (dedup) plus a row count (quota) —
    # narrow the SELECT instead of hydrating every active Document column.
    result = await db.execute(
        select(Document.content_hash).where(
            Document.tenant_id == user.tenant_id,
            Document.is_active == True,
        )
    )
    existing_hashes_raw = result.scalars().all()
    existing_count = len(existing_hashes_raw)

    max_mb = quota.max_file_size_mb if quota else settings.MAX_UPLOAD_SIZE_MB

    # Content-hash dedup: reject a byte-identical re-upload before it burns a
    # Cloudinary + Pinecone cost creating a second full set of chunks/vectors
    # for content that's already indexed. Same 409 precedent as the
    # duplicate-file_url check in select_sample_document.
    existing_hashes = {h for h in existing_hashes_raw if h}
    seen_hashes_in_batch: dict[str, str] = {}

    file_data = []
    total_new_storage = 0.0

    for file in files:
        if file.content_type not in ALLOWED_MIME_TYPES:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"File type '{file.content_type}' is not allowed. Allowed: PDF, DOCX, TXT, MD.",
            )

        content = await file.read()
        if len(content) == 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"'{file.filename}' is empty.",
            )
        sniff_mime_or_raise(content, file.content_type)

        filename = safe_filename(file.filename or "document")
        file_size_mb = len(content) / (1024 * 1024)

        content_hash = hashlib.sha256(content).hexdigest()
        if content_hash in existing_hashes:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"'{filename}' is identical to a document already in your workspace.",
            )
        if content_hash in seen_hashes_in_batch:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"'{filename}' is identical to '{seen_hashes_in_batch[content_hash]}' in this upload.",
            )
        seen_hashes_in_batch[content_hash] = filename

        # -1 means unlimited (see TenantQuota docstring)
        if max_mb != -1 and file_size_mb > max_mb:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"'{filename}' exceeds the {max_mb}MB per-file limit.",
            )

        total_new_storage += file_size_mb
        file_data.append((content, file_size_mb, filename, file.content_type, content_hash))

    if quota and quota.max_documents != -1 and (existing_count + len(files)) > quota.max_documents:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Document quota exceeded. Your plan allows {quota.max_documents} documents.",
        )

    saved_documents: List[Document] = []
    failures: List[str] = []

    for content, file_size_mb, filename, content_type, content_hash in file_data:
        public_id = None
        try:
            public_id, secure_url = await upload_file_to_cloudinary(
                file_content=content,
                tenant_id=user.tenant_id,
                original_filename=filename,
                content_type=content_type,
            )

            doc = Document(
                tenant_id=user.tenant_id,
                original_filename=filename,
                file_path=public_id,
                file_url=secure_url,
                file_size_mb=round(file_size_mb, 4),
                mime_type=content_type,
                source=DocumentSource.uploaded,
                status=DocumentStatus.pending,
                content_hash=content_hash,
            )
            db.add(doc)
            saved_documents.append(doc)
        except Exception as e:
            # Don't let one bad file abort the whole batch — but if a blob
            # already landed in Cloudinary before the failure, purge it so
            # nothing orphaned survives (no Document row will ever reference it).
            logger.error("Upload failed for '%s': %s", filename, e)
            if public_id:
                try:
                    await delete_file_from_cloudinary(public_id)
                except Exception:
                    logger.exception("Failed to clean up orphaned blob %s", public_id)
            failures.append(filename)

    await db.flush()

    if usage and saved_documents:
        actual_storage = sum(d.file_size_mb for d in saved_documents)
        usage.documents_count = (usage.documents_count or 0) + len(saved_documents)
        usage.storage_used_mb = round((usage.storage_used_mb or 0.0) + actual_storage, 4)

    await db.commit()

    for doc in saved_documents:
        await db.refresh(doc)

    if failures and not saved_documents:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"All uploads failed: {', '.join(failures)}",
        )

    logger.info(
        "Uploaded %d/%d file(s) to Cloudinary for tenant %s%s",
        len(saved_documents), len(files), user.tenant_id,
        f" ({len(failures)} failed)" if failures else "",
    )
    return saved_documents

async def select_sample_document(
    user: User, document_id: uuid.UUID, db: AsyncSession
) -> Document:
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

    quota = await lock_quota_row(user.tenant_id, db)
    if quota and quota.max_documents != -1:
        result = await db.execute(
            select(Document).where(
                Document.tenant_id == user.tenant_id,
                Document.is_active == True,
            )
        )
        existing_count = len(result.scalars().all())
        if existing_count + 1 > quota.max_documents:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Document quota exceeded. Your plan allows {quota.max_documents} documents.",
            )

    doc = Document(
        tenant_id=user.tenant_id,
        original_filename=source_doc.original_filename,
        file_path=source_doc.file_path,
        file_url=source_doc.file_url,
        file_size_mb=source_doc.file_size_mb,
        mime_type=source_doc.mime_type,
        source=DocumentSource.sample,
        status=DocumentStatus.pending,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    # Pinecone vectors are tenant-scoped (per-tenant namespace + tenant_id
    # metadata) — copying the Document row alone doesn't give this tenant
    # any searchable vectors, since the original vectors belong to
    # source_doc's tenant. Actually index it for the copying tenant, even
    # though it downloads/embeds the same shared file_url a second time.
    from app.services.indexing_service import schedule_document_processing
    schedule_document_processing([doc.id])

    logger.info(
        "Tenant %s selected community doc %s from tenant %s",
        user.tenant_id, document_id, source_doc.tenant_id,
    )
    return doc

async def get_tenant_documents(user: User, db: AsyncSession) -> List[Document]:
    result = await db.execute(
        select(Document).where(
            Document.tenant_id == user.tenant_id,
            Document.is_active == True,
        ).order_by(Document.created_at.desc())
    )
    return result.scalars().all()

async def select_platform_sample_documents(
    user: User, sample_document_ids: List[uuid.UUID], db: AsyncSession
) -> List[Document]:
    """
    Copies platform sample documents (SampleDocument records) into the user's workspace.
    Creates Document records for each selected SampleDocument, linking via sample_document_id.
    Used during onboarding when user selects "show me sample documents".
    """
    from app.models.sample_document import SampleDocument

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

    quota = await lock_quota_row(user.tenant_id, db)
    if quota and quota.max_documents != -1:
        result = await db.execute(
            select(Document).where(
                Document.tenant_id == user.tenant_id,
                Document.is_active == True,
            )
        )
        existing_count = len(result.scalars().all())
        if existing_count + len(sample_docs) > quota.max_documents:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Document quota exceeded. Your plan allows {quota.max_documents} documents.",
            )

    saved_documents = []
    for sample_doc in sample_docs:
        doc = Document(
            id=uuid.uuid4(),
            tenant_id=user.tenant_id,
            original_filename=sample_doc.filename,
            file_size_mb=sample_doc.file_size_mb,
            mime_type="application/pdf",
            source=DocumentSource.sample,
            status=DocumentStatus.pending,
            file_url=sample_doc.file_path,
            sample_document_id=sample_doc.id,
        )
        db.add(doc)
        saved_documents.append(doc)

    await db.flush()
    await db.commit()
    for doc in saved_documents:
        await db.refresh(doc)

    # Same reasoning as select_sample_document: these Document rows point at
    # a shared platform file, but need their own vectors indexed under this
    # tenant's Pinecone namespace before the chatbot can ever retrieve them.
    from app.services.indexing_service import schedule_document_processing
    schedule_document_processing([doc.id for doc in saved_documents])

    return saved_documents

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

    for url in urls:
        if not url.startswith(("http://", "https://")):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid URL: {url}. Must start with http:// or https://",
            )

    quota = await lock_quota_row(user.tenant_id, db)

    result = await db.execute(
        select(UsageCount).where(UsageCount.tenant_id == user.tenant_id)
        .order_by(UsageCount.period_month.desc())
    )
    usage = result.scalar_one_or_none()

    result = await db.execute(
        select(Document).where(
            Document.tenant_id == user.tenant_id,
            Document.is_active == True,
        )
    )
    existing_docs = result.scalars().all()
    existing_count = len(existing_docs)

    if quota and quota.max_documents != -1:
        if (existing_count + len(urls)) > quota.max_documents:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Document quota exceeded. Your plan allows {quota.max_documents} documents.",
            )

    max_mb = quota.max_file_size_mb if quota else settings.MAX_UPLOAD_SIZE_MB

    saved_documents: List[Document] = []

    for url in urls:
        # Tolerant like onboarding._scrape_urls_tolerant: one bad URL
        # shouldn't abort the rest of the batch — record it as `failed`
        # instead, so the dashboard still shows it and the good ones succeed.
        doc = Document(
            tenant_id=user.tenant_id,
            original_filename=url,
            file_size_mb=0.0,
            mime_type="application/pdf",
            source=DocumentSource.scraped,
            source_url=url,
            status=DocumentStatus.pending,
        )
        try:
            pdf_content, page_title = await scrape_website_to_pdf(
                url, timeout=settings.CRAWL4AI_TIMEOUT
            )

            file_size_mb = len(pdf_content) / (1024 * 1024)

            # -1 means unlimited (see TenantQuota docstring)
            if max_mb != -1 and file_size_mb > max_mb:
                raise ValueError(f"content exceeds the {max_mb}MB per-file limit after scraping")

            page_title = safe_filename(page_title)
            public_id, secure_url = await upload_file_to_cloudinary(
                file_content=pdf_content,
                tenant_id=user.tenant_id,
                original_filename=f"{page_title}.pdf",
                content_type="application/pdf",
            )

            doc.original_filename = page_title
            doc.file_path = public_id
            doc.file_url = secure_url
            doc.file_size_mb = round(file_size_mb, 4)

            logger.info(f"Scraped and added document from {url} for tenant {user.tenant_id}")
        except Exception as e:
            logger.error(f"Failed to scrape {url}: {str(e)}")
            doc.status = DocumentStatus.failed

        db.add(doc)
        saved_documents.append(doc)

    await db.flush()

    ready_documents = [d for d in saved_documents if d.status != DocumentStatus.failed]
    if usage and ready_documents:
        total_storage = sum(d.file_size_mb for d in ready_documents)
        usage.documents_count = (usage.documents_count or 0) + len(ready_documents)
        usage.storage_used_mb = round((usage.storage_used_mb or 0.0) + total_storage, 4)

    await db.commit()

    for doc in saved_documents:
        await db.refresh(doc)

    logger.info(
        "Scraped %d/%d website(s) for tenant %s",
        len(ready_documents), len(urls), user.tenant_id,
    )
    return saved_documents

async def add_faq_entries(
    user: User, entries: List[tuple[str, str]], db: AsyncSession
) -> List[Document]:
    """
    Adds one or more FAQ (question, answer) pairs as Document rows with
    source=faq — no file upload involved. Counts toward the same shared
    max_documents quota as PDFs/URLs (one document quota, not a separate
    FAQ quota). `entries` is expected to already be validated/sanitized at
    the API layer (FEAddFaqRequest handles empty/whitespace/length checks).
    """
    if not entries:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No FAQ entries provided.",
        )

    quota = await lock_quota_row(user.tenant_id, db)

    result = await db.execute(
        select(UsageCount).where(UsageCount.tenant_id == user.tenant_id)
        .order_by(UsageCount.period_month.desc())
    )
    usage = result.scalar_one_or_none()

    result = await db.execute(
        select(func.count()).select_from(Document).where(
            Document.tenant_id == user.tenant_id,
            Document.is_active == True,
        )
    )
    existing_count = result.scalar_one()

    if quota and quota.max_documents != -1 and (existing_count + len(entries)) > quota.max_documents:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Document quota exceeded. Your plan allows {quota.max_documents} documents.",
        )

    saved_documents: List[Document] = []
    for question, answer in entries:
        # Negligible size (a few hundred bytes to a few KB), but still flows
        # through the same UsageCount accounting path as everything else
        # rather than being special-cased out of it.
        size_mb = round(len((question + answer).encode("utf-8")) / (1024 * 1024), 6)
        doc = Document(
            tenant_id=user.tenant_id,
            original_filename=safe_filename(question, max_len=500),
            file_path=None,
            file_url=None,
            file_size_mb=size_mb,
            mime_type="text/plain",
            source=DocumentSource.faq,
            status=DocumentStatus.pending,
            question=question,
            answer=answer,
        )
        db.add(doc)
        saved_documents.append(doc)

    await db.flush()

    if usage and saved_documents:
        total_storage = sum(d.file_size_mb for d in saved_documents)
        usage.documents_count = (usage.documents_count or 0) + len(saved_documents)
        usage.storage_used_mb = round((usage.storage_used_mb or 0.0) + total_storage, 4)

    await db.commit()

    for doc in saved_documents:
        await db.refresh(doc)

    logger.info(
        "Added %d FAQ entr%s for tenant %s",
        len(saved_documents), "y" if len(saved_documents) == 1 else "ies", user.tenant_id,
    )
    return saved_documents

async def update_faq_entry(
    user: User, faq_id: uuid.UUID, question: str, answer: str, db: AsyncSession
) -> Document:
    """
    Edits an existing FAQ entry and re-schedules it for indexing. Purges its
    old Pinecone vectors first — otherwise, if the new content produces
    fewer chunks than the old content did, the extra old chunk vectors are
    never overwritten and stay searchable even though they no longer match
    what the FAQ actually says.
    """
    result = await db.execute(
        select(Document).where(
            Document.id == faq_id,
            Document.tenant_id == user.tenant_id,
            Document.source == DocumentSource.faq,
            Document.is_active == True,
        )
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="FAQ entry not found.")

    try:
        await vector_store.delete_document_chunks(str(user.tenant_id), str(doc.id))
    except Exception:
        logger.exception("Failed to purge old vectors before re-indexing FAQ %s", doc.id)

    old_size_mb = doc.file_size_mb or 0.0
    new_size_mb = round(len((question + answer).encode("utf-8")) / (1024 * 1024), 6)

    doc.question = question
    doc.answer = answer
    doc.original_filename = safe_filename(question, max_len=500)
    doc.file_size_mb = new_size_mb
    doc.status = DocumentStatus.pending
    doc.chunk_count = None

    result = await db.execute(
        select(UsageCount).where(UsageCount.tenant_id == user.tenant_id)
        .order_by(UsageCount.period_month.desc())
    )
    usage = result.scalar_one_or_none()
    if usage is not None:
        usage.storage_used_mb = round(
            max(0.0, (usage.storage_used_mb or 0.0) - old_size_mb + new_size_mb), 4
        )

    await db.commit()
    await db.refresh(doc)
    return doc
