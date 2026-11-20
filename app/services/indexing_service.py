"""
Indexing Service — turns stored documents into indexed, chunked knowledge.

Runs as a fire-and-forget background task after every upload / URL scrape
(and on "Re-index all"), so the Knowledge page's status moves
Processing → Indexed automatically:

  1. Download the file from Cloudinary (files are Fernet-encrypted at rest)
  2. Extract text (PDF via PyPDF2, plain text as-is)
  3. Chunk into ~RAG_CHUNK_SIZE-token pieces
  4. If Pinecone is configured, embed + upsert the chunks for RAG search
  5. Persist chunk_count and set status = ready (or failed on error)

Without a PINECONE_API_KEY the vector upsert is skipped — chunk counts and
statuses still update so the dashboard works end-to-end.
"""

import asyncio
import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, select
from sqlalchemy import update as sa_update

from app.config import settings
from app.core.chunking import chunk_pdf_text
from app.core.storage import decrypt_file
from app.database import AsyncSessionLocal
from app.models.document import Document, DocumentSource, DocumentStatus
from app.models.tenant_quota import TenantQuota

logger = logging.getLogger(__name__)

def schedule_document_processing(document_ids: list[uuid.UUID]) -> None:
    """Kick off background processing for the given documents (non-blocking)."""
    for doc_id in document_ids:
        asyncio.create_task(_process_document_safe(doc_id))

def schedule_url_scrape(document_ids: list[uuid.UUID]) -> None:
    """
    Kick off background scrape + index for freshly-created URL documents
    (non-blocking). Unlike schedule_document_processing, these documents
    have no file_url yet — the scrape itself (headless browser, up to
    CRAWL4AI_TIMEOUT seconds) used to run synchronously inside the HTTP
    request that created them; it now runs here instead, off the request
    path, same as the chunk/embed step already did.
    """
    for doc_id in document_ids:
        asyncio.create_task(_scrape_and_process_url_safe(doc_id))

async def _scrape_and_process_url_safe(document_id: uuid.UUID) -> None:
    try:
        scraped = await _scrape_url_into_document(document_id)
    except Exception as e:
        logger.exception("Scrape failed for document %s", document_id)
        try:
            async with AsyncSessionLocal() as db:
                doc = (
                    await db.execute(select(Document).where(Document.id == document_id))
                ).scalar_one_or_none()
                if doc is not None:
                    doc.status = DocumentStatus.failed
                    doc.error_message = _failure_reason(e)
                    await db.commit()
        except Exception:
            logger.error("Could not mark document %s failed after scrape error", document_id)
        return
    if scraped:
        await _process_document_safe(document_id)

async def _scrape_url_into_document(document_id: uuid.UUID) -> bool:
    """Scrapes doc.source_url to a PDF, uploads it to Cloudinary, and fills
    in file_url/file_path/file_size_mb. Returns False (document left as-is)
    if the document has vanished or has no source_url to scrape."""
    from app.core.scraper import scrape_website
    from app.core.storage import upload_file_to_cloudinary
    from app.core.validation import safe_filename

    from app.models.usage_count import UsageCount

    async with AsyncSessionLocal() as db:
        doc = (
            await db.execute(select(Document).where(Document.id == document_id))
        ).scalar_one_or_none()
        if doc is None or not doc.source_url:
            return False
        url = doc.source_url
        tenant_id = doc.tenant_id

        quota_result = await db.execute(
            select(TenantQuota).where(TenantQuota.tenant_id == tenant_id)
        )
        quota = quota_result.scalar_one_or_none()
        max_mb = quota.max_file_size_mb if quota else settings.MAX_UPLOAD_SIZE_MB
        # Plan-wide storage cap, checked against current usage once the
        # scraped size is known — mirrors the upload path, which already
        # refuses files that would blow past max_storage_mb. Without this,
        # URL scrapes were the one ingestion path that could grow storage
        # unbounded.
        max_storage = quota.max_storage_mb if quota else -1
        usage_row = (
            await db.execute(
                select(UsageCount).where(UsageCount.tenant_id == tenant_id)
                .order_by(UsageCount.period_month.desc())
            )
        ).scalars().first()
        storage_used = (usage_row.storage_used_mb if usage_row else 0.0) or 0.0

    # OCR of on-page images costs a Gemini call per image — only when the
    # feature is on and the tenant's trial/wallet would allow an LLM call.
    ocr_allowed = await _ocr_allowed(tenant_id)
    ocr_usages: list = []
    scraped_content, page_title, mime_type, etag, last_modified = await scrape_website(
        url, timeout=settings.CRAWL4AI_TIMEOUT, ocr=ocr_allowed, ocr_usages=ocr_usages,
    )
    await _record_ocr_usages(tenant_id, ocr_usages)
    file_size_mb = len(scraped_content) / (1024 * 1024)
    # -1 means unlimited (see TenantQuota docstring)
    if max_mb != -1 and file_size_mb > max_mb:
        raise ValueError(f"content exceeds the {max_mb}MB per-file limit after scraping")
    if max_storage != -1 and storage_used + file_size_mb > max_storage:
        raise ValueError(
            f"scraped content ({file_size_mb:.1f}MB) would exceed the plan's "
            f"{max_storage}MB storage limit ({storage_used:.1f}MB already used)"
        )

    page_title = safe_filename(page_title)
    extension = ".pdf" if mime_type == "application/pdf" else ".md"
    public_id, secure_url = await upload_file_to_cloudinary(
        file_content=scraped_content,
        tenant_id=tenant_id,
        original_filename=f"{page_title}{extension}",
        content_type=mime_type,
    )

    async with AsyncSessionLocal() as db:
        doc = (
            await db.execute(select(Document).where(Document.id == document_id))
        ).scalar_one_or_none()
        if doc is None:
            return False
        old_size_mb = doc.file_size_mb or 0.0
        doc.original_filename = page_title
        doc.file_path = public_id
        doc.file_url = secure_url
        doc.file_size_mb = round(file_size_mb, 4)
        doc.mime_type = mime_type
        # Refresh-sweep baseline (see scrape_refresh_sweep_loop) — whatever
        # the page's own response sent, plus a content hash as the fallback
        # "did it actually change" check for sites with neither header.
        doc.etag = etag
        doc.last_modified_header = last_modified
        doc.content_hash = hashlib.sha256(scraped_content).hexdigest()
        doc.last_checked_at = datetime.now(timezone.utc)

        # Book the scraped size against the tenant's storage usage (delta, so
        # a re-scrape doesn't double-count). Uploads/FAQs/samples all update
        # storage_used_mb at creation; URL documents are created at 0.0MB and
        # only get a real size here, after the background scrape.
        usage = (
            await db.execute(
                select(UsageCount).where(UsageCount.tenant_id == doc.tenant_id)
                .order_by(UsageCount.period_month.desc())
            )
        ).scalars().first()
        if usage is not None:
            usage.storage_used_mb = round(
                max(0.0, (usage.storage_used_mb or 0.0) - old_size_mb + file_size_mb), 4
            )
        await db.commit()

    return True

async def _ocr_allowed(tenant_id) -> bool:
    """OCR is a paid Gemini call per image/PDF: allowed only when the feature
    flag is on AND the tenant could make an LLM call right now (inside the
    signup trial, or with wallet balance) — the same gate as a chat answer,
    so an empty wallet can't go negative on a page full of pictures."""
    if not settings.SCRAPE_OCR_ENABLED or not settings.GEMINI_API_KEY:
        return False
    from app.models.tenant import Tenant
    from app.services import wallet_service

    tid = uuid.UUID(str(tenant_id))
    try:
        async with AsyncSessionLocal() as db:
            tenant = await db.get(Tenant, tid)
            if tenant is None:
                return False
            gate = await wallet_service.check_can_generate(tenant, wallet_service.DEFAULT_PROVIDER, db)
            await db.commit()  # persists a trial_ended_at flip, if any
            if not gate.allowed:
                logger.info("OCR skipped for tenant %s: %s", tenant_id, gate.block_reason)
            return gate.allowed
    except Exception:
        logger.exception("Could not evaluate OCR gate for tenant %s; skipping OCR", tenant_id)
        return False


async def _record_ocr_usages(tenant_id, usages: list) -> None:
    """Bill every OCR call through the wallet (free while the tenant is in
    trial, otherwise real cost × markup). Own session + own try/except so a
    billing hiccup never fails the document being indexed."""
    if not usages:
        return
    from app.services import wallet_service

    tid = uuid.UUID(str(tenant_id))
    try:
        async with AsyncSessionLocal() as db:
            for usage in usages:
                await wallet_service.record_usage(
                    tenant_id=tid, conversation_id=None, call_type="ocr", usage=usage, db=db,
                )
            await db.commit()
    except Exception:
        logger.exception("Failed to record %d OCR usage row(s) for tenant %s", len(usages), tenant_id)


async def stale_document_sweep_loop() -> None:
    """
    Recurring background sweep, started once from the app lifespan (as a
    fire-and-forget task, not awaited inline). A single startup-only sweep
    means a document stuck mid-index only recovers on the *next process
    restart*, which could be arbitrarily far away — this loop instead
    re-checks every INDEXING_STALE_MINUTES for the life of the process, so
    recovery doesn't depend on a restart ever happening.
    """
    while True:
        try:
            await recover_stuck_documents()
        except Exception:
            logger.exception("Stale-document sweep iteration failed")
        await asyncio.sleep(settings.INDEXING_STALE_MINUTES * 60)

async def recover_stuck_documents() -> None:
    """
    Self-heal sweep (see stale_document_sweep_loop for the recurring
    caller). Indexing runs as an in-process asyncio task with no durable
    queue behind it, so a process restart/crash — or, absent this recurring
    loop, simply time passing — while a document is mid-index leaves it
    stuck in `processing` forever unless something resets it. Reset anything
    stuck past INDEXING_STALE_MINUTES back to `pending` and reschedule it.

    This is a lightweight, appropriately-scoped substitute for a durable job
    queue (Celery/Redis, or a Postgres `SELECT ... FOR UPDATE SKIP LOCKED`
    job table) — sufficient for this deployment's scale; a genuinely durable
    queue would be the next step if that's ever needed, but isn't required
    here.
    """
    threshold = datetime.now(timezone.utc) - timedelta(minutes=settings.INDEXING_STALE_MINUTES)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Document.id).where(
                Document.status == DocumentStatus.processing,
                Document.updated_at < threshold,
            )
        )
        stuck_ids = [row[0] for row in result.all()]
        if not stuck_ids:
            return
        await db.execute(
            sa_update(Document)
            .where(Document.id.in_(stuck_ids))
            .values(status=DocumentStatus.pending)
        )
        await db.commit()

    logger.warning("Recovered %d document(s) stuck in processing; rescheduling", len(stuck_ids))
    schedule_document_processing(stuck_ids)

# How often the loop wakes up to look for due documents — independent of
# SCRAPE_REFRESH_INTERVAL_DAYS (the per-document interval), same idea as
# stale_document_sweep_loop checking far more often than any one document
# actually needs it, so a freshly-due document doesn't wait a whole cycle.
_REFRESH_SWEEP_CHECK_INTERVAL_SECONDS = 6 * 3600

async def scrape_refresh_sweep_loop() -> None:
    """
    Recurring background sweep, started once from the app lifespan. Keeps
    already-indexed scraped URLs current without the tenant having to
    manually re-add them — see app/core/scraper.check_for_update for the
    actual conditional-GET probe.
    """
    while True:
        try:
            await refresh_due_scraped_documents()
        except Exception:
            logger.exception("Scrape-refresh sweep iteration failed")
        await asyncio.sleep(_REFRESH_SWEEP_CHECK_INTERVAL_SECONDS)

async def refresh_due_scraped_documents() -> None:
    """
    One sweep pass: every active, successfully-indexed scraped document not
    checked in the last SCRAPE_REFRESH_INTERVAL_DAYS gets a cheap conditional
    probe. Unchanged (a 304, or a 200 whose body hash matches what's already
    indexed) just bumps last_checked_at. Changed — or the probe itself
    failed/was blocked — is handled without ever marking a working document
    failed: a changed page is rescheduled through the exact same pipeline a
    brand-new URL goes through (full re-scrape, re-chunk, re-embed); a probe
    that couldn't complete (network hiccup, now-disallowed robots.txt, site
    down) just tries again next cycle, since nothing here is evidence the
    existing indexed content has gone stale.
    """
    if not settings.SCRAPE_REFRESH_ENABLED:
        return
    from app.core.scraper import check_for_update

    threshold = datetime.now(timezone.utc) - timedelta(days=settings.SCRAPE_REFRESH_INTERVAL_DAYS)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Document).where(
                Document.source == DocumentSource.scraped,
                Document.is_active == True,
                Document.status == DocumentStatus.ready,
                Document.source_url.isnot(None),
                (Document.last_checked_at.is_(None)) | (Document.last_checked_at < threshold),
            )
        )
        due = list(result.scalars().all())

    if not due:
        return
    logger.info("Scrape-refresh sweep: checking %d due document(s)", len(due))

    changed_ids: list[uuid.UUID] = []
    for doc in due:
        probe = await check_for_update(doc.source_url, doc.etag, doc.last_modified_header)
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(Document).where(Document.id == doc.id))).scalar_one_or_none()
            if row is None or row.status != DocumentStatus.ready:
                continue  # vanished, or a scrape/edit raced ahead of this sweep
            if probe is None:
                # Couldn't check this cycle — leave content alone, but don't
                # retry every single sweep tick either; try again next interval.
                row.last_checked_at = datetime.now(timezone.utc)
                await db.commit()
                continue
            if probe["not_modified"] or probe["content_hash"] == row.content_hash:
                row.etag = probe["etag"] or row.etag
                row.last_modified_header = probe["last_modified"] or row.last_modified_header
                row.last_checked_at = datetime.now(timezone.utc)
                await db.commit()
                continue
            # Genuinely changed — the actual re-scrape (scheduled below,
            # outside this session) will overwrite etag/last_modified/
            # content_hash/last_checked_at itself once it succeeds.
            changed_ids.append(row.id)

    if changed_ids:
        logger.info("Scrape-refresh sweep: %d document(s) changed, re-indexing", len(changed_ids))
        schedule_url_scrape(changed_ids)

def _failure_reason(exc: BaseException) -> str:
    """Short, user-readable reason stored on the document (see
    Document.error_message). Maps the common parser errors to plain words;
    everything else keeps the exception's own message, truncated."""
    name = type(exc).__name__
    text = str(exc).strip() or name
    if "PdfReadError" in name or "startxref" in text or "EOF marker" in text:
        return "The PDF appears to be corrupt or isn't a real PDF file."
    if "Timeout" in name or "timed out" in text.lower():
        return "Timed out while reading the source — try re-indexing."
    return f"{name}: {text}"[:500]

async def _process_document_safe(document_id: uuid.UUID) -> None:
    try:
        await _process_document(document_id)
    except Exception as e:
        logger.exception("Indexing failed for document %s", document_id)
        try:
            async with AsyncSessionLocal() as db:
                doc = (
                    await db.execute(select(Document).where(Document.id == document_id))
                ).scalar_one_or_none()
                if doc is not None:
                    doc.status = DocumentStatus.failed
                    doc.error_message = _failure_reason(e)
                    await db.commit()
        except Exception as mark_error:
            logger.error("Could not mark document %s failed: %s", document_id, mark_error)

async def _process_document(document_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        doc = (
            await db.execute(select(Document).where(Document.id == document_id))
        ).scalar_one_or_none()
        if doc is None:
            logger.warning("Document %s vanished before indexing", document_id)
            return
        if doc.source != DocumentSource.faq and not doc.file_url:
            raise ValueError("Document has no file_url to download")
        if doc.status == DocumentStatus.processing:
            logger.info("Document %s is already processing — skipping duplicate schedule", document_id)
            return

        # Atomic claim: only flip to `processing` if it isn't already, so two
        # concurrent schedules for the same document (e.g. an upload landing
        # at the same moment as "Re-index all") can't both start indexing it
        # and race their status updates against each other.
        claim = await db.execute(
            sa_update(Document)
            .where(Document.id == document_id, Document.status != DocumentStatus.processing)
            .values(status=DocumentStatus.processing)
        )
        await db.commit()
        if claim.rowcount == 0:
            logger.info("Document %s claimed by another run — skipping", document_id)
            return

        source = doc.source
        file_url = doc.file_url
        file_path = doc.file_path  # Cloudinary public_id
        mime_type = doc.mime_type
        tenant_id = str(doc.tenant_id)
        question = doc.question
        answer = doc.answer
        # chunk_count is only ever set after a successful index, so None
        # here means this document has never reached Pinecone before —
        # skip the purge below rather than paying for a delete call with
        # nothing to delete.
        previously_indexed = doc.chunk_count is not None

    if source == DocumentSource.faq:
        # No file to download/decrypt/extract — the Q&A text itself is
        # already all there is; go straight to chunking.
        text, page_map = f"Q: {question}\nA: {answer}", []
    else:
        content = await _download(file_url, file_path)

        # Files uploaded through app.core.storage are Fernet-encrypted at rest;
        # tolerate unencrypted content (e.g. seeded/sample files).
        try:
            content = await decrypt_file(content)
        except Exception:
            logger.info("Document %s is not encrypted — using raw bytes", document_id)

        text, page_map = await asyncio.to_thread(_extract_text, content, mime_type)

        # A PDF with no text layer (a scan, a photographed brochure) yields
        # nothing above. Read it with Gemini vision instead — same gate and
        # billing as image OCR on scraped pages.
        is_pdf = mime_type == "application/pdf" or content[:5] == b"%PDF-"
        if not text.strip() and is_pdf and await _ocr_allowed(tenant_id):
            from app.core.ocr import extract_text_from_pdf

            try:
                text, usage = await extract_text_from_pdf(content)
                await _record_ocr_usages(tenant_id, [usage] if usage else [])
                page_map = []
                logger.info("OCR transcribed scanned PDF %s: %d chars", document_id, len(text))
            except Exception as e:
                logger.warning("Scanned-PDF OCR failed for %s: %s", document_id, e)

    if not text.strip():
        raise ValueError("No text could be extracted from the document")

    chunks = await asyncio.to_thread(
        chunk_pdf_text,
        text,
        chunk_size=settings.RAG_CHUNK_SIZE,
        overlap_size=settings.RAG_CHUNK_OVERLAP,
        page_map=page_map,
    )
    if not chunks:
        raise ValueError("No chunks generated")

    # Word count on the full extracted text, not summed per chunk — chunk
    # overlap would otherwise double-count the overlapping words. This is
    # the plan's "knowledge size" meter (file-size-independent; a scanned
    # PDF can hold little text while a small text file holds a lot) — see
    # Entitlements.max_words.
    word_count = len(text.split())
    await _enforce_knowledge_limits(tenant_id, document_id, word_count, len(chunks))

    if settings.PINECONE_API_KEY:
        from app.core.embeddings import embed_chunks
        from app.core.vector_store import delete_document_chunks, upsert_chunks

        # Best-effort purge of any vectors from a previous index of this same
        # document before writing the new set — otherwise a re-index that
        # produces fewer chunks than last time leaves old higher-sequence
        # vectors behind as permanently-orphaned stale search results.
        # Skipped entirely for first-time indexing, where there's nothing to purge.
        if previously_indexed:
            try:
                await delete_document_chunks(tenant_id, str(document_id))
            except Exception as e:
                logger.warning("Pre-reindex vector purge failed for %s: %s", document_id, e)

        # Deliberately NOT caught here: a failed embed/upsert must fail the
        # whole document (propagates to _process_document_safe's except
        # block, which marks status=failed) rather than being downgraded to
        # a warning — otherwise the document is marked "ready" with a
        # chunk_count but zero vectors ever reached Pinecone, and the
        # chatbot silently returns "no relevant documents" forever with no
        # visible error anywhere.
        embeddings = await embed_chunks([c["text"] for c in chunks])
        await upsert_chunks(tenant_id, str(document_id), chunks, embeddings)
        logger.info("Upserted %d chunks to Pinecone for %s", len(chunks), document_id)
    else:
        logger.info("PINECONE_API_KEY not set — skipping vector upsert for %s", document_id)

    async with AsyncSessionLocal() as db:
        doc = (
            await db.execute(select(Document).where(Document.id == document_id))
        ).scalar_one_or_none()
        if doc is None:
            return
        # Mirror chunk text into Postgres for the keyword half of hybrid search.
        # Runs regardless of whether Pinecone is configured, so lexical search
        # works even without a vector store. Replace any prior rows first so a
        # re-index doesn't leave stale chunks behind.
        await _write_keyword_chunks(str(doc.tenant_id), document_id, chunks, db)
        doc.chunk_count = len(chunks)
        doc.word_count = word_count
        doc.status = DocumentStatus.ready
        await db.commit()

    logger.info("Indexed document %s: %d chunks, %d words", document_id, len(chunks), word_count)

async def _enforce_knowledge_limits(
    tenant_id: str, document_id: uuid.UUID, new_words: int, new_chunks: int
) -> None:
    """
    Raises ValueError (→ status=failed, same as any other indexing failure)
    if indexing this document would push the tenant over its plan's word or
    chunk ceiling. Checked here, after chunking but before the Pinecone
    embed/upsert call, so an over-limit document never pays for or reaches
    the vector store. Both totals are summed live across the tenant's other
    active documents (excluding this one, which has no word/chunk_count yet)
    rather than an incremental counter, so there's nothing to drift.
    """
    from sqlalchemy import func

    from app.core.entitlements import get_entitlements
    from app.models.subscription import Subscription

    async with AsyncSessionLocal() as db:
        subscription = (
            await db.execute(select(Subscription).where(Subscription.tenant_id == tenant_id))
        ).scalar_one_or_none()
        ent = get_entitlements(subscription)

        existing_words, existing_chunks = (
            await db.execute(
                select(
                    func.coalesce(func.sum(Document.word_count), 0),
                    func.coalesce(func.sum(Document.chunk_count), 0),
                ).where(
                    Document.tenant_id == tenant_id,
                    Document.is_active == True,
                    Document.id != document_id,
                )
            )
        ).one()

    if ent.max_words != -1 and existing_words + new_words > ent.max_words:
        raise ValueError(
            f"Adding this document would exceed your plan's {ent.max_words:,}-word "
            f"knowledge limit ({existing_words:,} words already used)."
        )
    if ent.max_chunks != -1 and existing_chunks + new_chunks > ent.max_chunks:
        raise ValueError(
            f"Adding this document would exceed your plan's {ent.max_chunks:,}-chunk "
            f"limit ({existing_chunks:,} chunks already used)."
        )

async def _write_keyword_chunks(tenant_id: str, document_id, chunks: list[dict], db) -> None:
    """Replace the Postgres keyword-search rows for a document with the current
    chunk set. Text is capped to keep a pathological chunk from bloating the row
    (the same 8000-char cap the Pinecone metadata uses)."""
    from app.models.document_chunk import DocumentChunk

    await db.execute(
        delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
    )
    for c in chunks:
        db.add(DocumentChunk(
            tenant_id=uuid.UUID(tenant_id) if isinstance(tenant_id, str) else tenant_id,
            document_id=document_id,
            chunk_id=str(c.get("chunk_id", c.get("sequence", 0))),
            sequence=int(c.get("sequence", 0)),
            text=(c["text"] or "")[:8000],
        ))

async def _download(url: str, public_id: str = None) -> bytes:
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resp = await client.get(url)
        if resp.status_code in (401, 403) and public_id:
            # Cloudinary restricts anonymous delivery of raw files on this
            # account — fetch through the API-key-signed download URL instead.
            signed_url = _signed_download_url(public_id)
            resp = await client.get(signed_url)
        resp.raise_for_status()
        return resp.content

def _signed_download_url(public_id: str) -> str:
    import cloudinary
    import cloudinary.utils

    cloudinary.config(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME,
        api_key=settings.CLOUDINARY_API_KEY,
        api_secret=settings.CLOUDINARY_API_SECRET,
        secure=True,
    )
    return cloudinary.utils.private_download_url(
        public_id, format=None, resource_type="raw", type="upload"
    )

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

def _extract_text(content: bytes, mime_type: str) -> tuple[str, list[tuple[int, int]]]:
    """
    Returns (text, page_map). page_map is [(char_start, page_number), ...]
    for PDFs (empty for everything else — DOCX/TXT/MD have no page concept
    at this stage). Runs on a worker thread (see asyncio.to_thread call
    site) since PyPDF2/python-docx parsing and tiktoken chunking are both
    CPU-bound and would otherwise block the event loop.
    """
    if mime_type == "application/pdf" or content[:5] == b"%PDF-":
        import io

        from PyPDF2 import PdfReader

        reader = PdfReader(io.BytesIO(content))
        parts = []
        page_map: list[tuple[int, int]] = []
        offset = 0
        for page_num, page in enumerate(reader.pages, start=1):
            page_text = page.extract_text() or ""
            if page_text.strip():
                page_map.append((offset, page_num))
                parts.append(page_text)
                offset += len(page_text) + 2  # +2 accounts for the "\n\n" joiner below
        return "\n\n".join(parts), page_map

    if mime_type == _DOCX_MIME or content[:4] == b"PK\x03\x04":
        import io

        from docx import Document as DocxDocument

        docx_doc = DocxDocument(io.BytesIO(content))
        parts = [p.text for p in docx_doc.paragraphs if p.text.strip()]
        return "\n\n".join(parts), []

    # Fall back to treating the payload as UTF-8 text (TXT/MD uploads).
    return content.decode("utf-8", errors="ignore"), []
