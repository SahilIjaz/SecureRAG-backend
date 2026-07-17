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

import httpx
from sqlalchemy import select

from app.config import settings
from app.core.chunking import chunk_pdf_text
from app.core.storage import decrypt_file
from app.database import AsyncSessionLocal
from app.models.document import Document, DocumentStatus

logger = logging.getLogger(__name__)

def schedule_document_processing(document_ids: list[uuid.UUID]) -> None:
    """Kick off background processing for the given documents (non-blocking)."""
    for doc_id in document_ids:
        asyncio.create_task(_process_document_safe(doc_id))

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
        if not doc.file_url:
            raise ValueError("Document has no file_url to download")

        doc.status = DocumentStatus.processing
        await db.commit()

        file_url = doc.file_url
        file_path = doc.file_path  # Cloudinary public_id
        mime_type = doc.mime_type
        tenant_id = str(doc.tenant_id)

    content = await _download(file_url, file_path)

    # Files uploaded through app.core.storage are Fernet-encrypted at rest;
    # tolerate unencrypted content (e.g. seeded/sample files).
    try:
        content = await decrypt_file(content)
    except Exception:
        logger.info("Document %s is not encrypted — using raw bytes", document_id)

    text = _extract_text(content, mime_type)
    if not text.strip():
        raise ValueError("No text could be extracted from the document")

    chunks = chunk_pdf_text(
        text,
        chunk_size=settings.RAG_CHUNK_SIZE,
        overlap_size=settings.RAG_CHUNK_OVERLAP,
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

def _extract_text(content: bytes, mime_type: str) -> str:
    if mime_type == "application/pdf" or content[:5] == b"%PDF-":
        import io

        from PyPDF2 import PdfReader

        reader = PdfReader(io.BytesIO(content))
        parts = []
        for page in reader.pages:
            page_text = page.extract_text() or ""
            if page_text.strip():
                parts.append(page_text)
        return "\n\n".join(parts)

    # Fall back to treating the payload as UTF-8 text (TXT/MD uploads).
    return content.decode("utf-8", errors="ignore")
