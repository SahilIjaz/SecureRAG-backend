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
import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy import update as sa_update

from app.config import settings
from app.core.chunking import chunk_pdf_text
from app.core.storage import decrypt_file
from app.database import AsyncSessionLocal
from app.models.document import Document, DocumentSource, DocumentStatus

logger = logging.getLogger(__name__)

def schedule_document_processing(document_ids: list[uuid.UUID]) -> None:
    """Kick off background processing for the given documents (non-blocking)."""
    for doc_id in document_ids:
        asyncio.create_task(_process_document_safe(doc_id))

async def recover_stuck_documents() -> None:
    """
    Startup self-heal, called once from the app lifespan. Indexing runs as
    an in-process asyncio task with no durable queue behind it, so a process
    restart/crash while a document is mid-index leaves it stuck in
    `processing` forever — nothing else would ever pick it back up. Reset
    anything stuck past INDEXING_STALE_MINUTES back to `pending` and
    reschedule it.

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

async def _process_document_safe(document_id: uuid.UUID) -> None:
    try:
        await _process_document(document_id)
    except Exception:
        logger.exception("Indexing failed for document %s", document_id)
        try:
            async with AsyncSessionLocal() as db:
                doc = (
                    await db.execute(select(Document).where(Document.id == document_id))
                ).scalar_one_or_none()
                if doc is not None:
                    doc.status = DocumentStatus.failed
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

    if settings.PINECONE_API_KEY:
        try:
            from app.core.embeddings import embed_chunks
            from app.core.vector_store import upsert_chunks

            embeddings = await embed_chunks([c["text"] for c in chunks])
            await upsert_chunks(tenant_id, str(document_id), chunks, embeddings)
            logger.info("Upserted %d chunks to Pinecone for %s", len(chunks), document_id)
        except Exception as e:
            # Vector search is an enhancement — chunk counting still succeeds.
            logger.warning("Pinecone upsert failed for %s: %s", document_id, e)
    else:
        logger.info("PINECONE_API_KEY not set — skipping vector upsert for %s", document_id)

    async with AsyncSessionLocal() as db:
        doc = (
            await db.execute(select(Document).where(Document.id == document_id))
        ).scalar_one_or_none()
        if doc is None:
            return
        doc.chunk_count = len(chunks)
        doc.status = DocumentStatus.ready
        await db.commit()

    logger.info("Indexed document %s: %d chunks", document_id, len(chunks))

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
