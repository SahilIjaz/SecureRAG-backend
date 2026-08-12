"""Pinecone vector store for document chunks."""

import asyncio
import logging
import uuid
from typing import List, Dict, Any
from pinecone import Pinecone

from app.config import settings
from app.core.retry import retry_async

logger = logging.getLogger(__name__)

_pc = None
# Set of index names whose dimension has already been verified this process
# lifetime — avoids an extra describe_index() call on every get_index().
_dimension_verified: set[str] = set()

def _get_pinecone():
    """Lazy-load Pinecone client."""
    global _pc
    if _pc is None:
        _pc = Pinecone(api_key=settings.PINECONE_API_KEY)
    return _pc

def ensure_index(index_name: str = None) -> None:
    """Create the serverless index if it doesn't exist (idempotent). If it
    already exists, verify its dimension matches EMBEDDING_DIMENSION — a
    mismatch (e.g. a leftover index from an older embedding model) would
    otherwise make every upsert/query fail, and without this check that
    failure is easy to misdiagnose since it surfaces deep inside Pinecone's
    client rather than as a clear startup error."""
    from app.core.embeddings import EMBEDDING_DIMENSION

    from pinecone import ServerlessSpec

    name = index_name or settings.PINECONE_INDEX_NAME
    pc = _get_pinecone()
    if name not in pc.list_indexes().names():
        logger.info("Creating Pinecone index %s (dim=%d)", name, EMBEDDING_DIMENSION)
        pc.create_index(
            name=name,
            dimension=EMBEDDING_DIMENSION,
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1"),
        )
        _dimension_verified.add(name)
        return

    if name in _dimension_verified:
        return
    actual_dimension = pc.describe_index(name).dimension
    if actual_dimension != EMBEDDING_DIMENSION:
        raise RuntimeError(
            f"Pinecone index '{name}' has dimension {actual_dimension}, but the "
            f"embedding model ({EMBEDDING_DIMENSION}-dim) requires a matching index. "
            "Every upsert/query against this index will fail until this is resolved "
            "(delete and recreate the index, or point PINECONE_INDEX_NAME elsewhere)."
        )
    _dimension_verified.add(name)

def get_index(index_name: str = None):
    """Get Pinecone index (creating it on first use)."""
    name = index_name or settings.PINECONE_INDEX_NAME
    ensure_index(name)
    pc = _get_pinecone()
    return pc.Index(name)

# Cap on concurrent in-flight upsert batches — mirrors embeddings.py's
# _MAX_CONCURRENT_BATCHES for the same reason (parallelize large documents,
# don't hammer Pinecone's rate limits).
_MAX_CONCURRENT_UPSERT_BATCHES = 4

async def upsert_chunks(
    tenant_id: str,
    document_id: str,
    chunks: List[Dict[str, Any]],
    embeddings: List[List[float]],
) -> None:
    """
    Upsert document chunks to Pinecone, into the tenant's namespace.

    Args:
        tenant_id: Tenant ID — also the Pinecone namespace (structural tenant
            isolation; the tenant_id metadata field/filter below is kept too,
            as a second, redundant layer of defense-in-depth)
        document_id: Document ID
        chunks: List of chunk metadata
        embeddings: List of embeddings (one per chunk)
    """
    index = get_index()

    vectors_to_upsert = []

    for chunk, embedding in zip(chunks, embeddings):
        vector_id = f"{tenant_id}#{document_id}#{chunk['chunk_id']}"

        metadata = {
            "tenant_id": tenant_id,
            "document_id": document_id,
            "chunk_id": chunk["chunk_id"],
            "sequence": chunk["sequence"],
            # Full chunk text (capped well under Pinecone's 40KB metadata limit)
            # — this is the context handed to the LLM at answer time.
            "text": chunk["text"][:8000],
            "token_count": chunk["token_count"],
        }
        # Best-effort citation metadata (PDF page number / char offset within
        # the extracted text) — only present when the source document had a
        # page_map (see chunk_pdf_text). Pinecone metadata can't hold `None`,
        # so omit the key entirely rather than sending a null.
        if chunk.get("page") is not None:
            metadata["page"] = chunk["page"]
        if chunk.get("char_offset") is not None:
            metadata["char_offset"] = chunk["char_offset"]

        vectors_to_upsert.append((vector_id, embedding, metadata))

    batch_size = 100
    batches = [vectors_to_upsert[i:i + batch_size] for i in range(0, len(vectors_to_upsert), batch_size)]
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_UPSERT_BATCHES)

    async def _upsert_batch(batch_num: int, batch: list) -> None:
        async with semaphore:
            await retry_async(asyncio.to_thread, index.upsert, vectors=batch, namespace=tenant_id)
            logger.info(f"Upserted batch {batch_num} of chunks")

    await asyncio.gather(*(_upsert_batch(i + 1, batch) for i, batch in enumerate(batches)))

    logger.info(f"Upserted {len(vectors_to_upsert)} chunks for document {document_id}")

async def search_chunks(
    query_embedding: List[float],
    tenant_id: str,
    top_k: int = 5,
) -> List[Dict[str, Any]]:
    """
    Search for similar chunks using vector similarity, scoped to the
    tenant's namespace (plus a redundant metadata filter — see upsert_chunks).

    Args:
        query_embedding: Embedding of the query
        tenant_id: Tenant ID for filtering
        top_k: Number of top results to return

    Returns:
        List of similar chunks with metadata
    """
    index = get_index()

    results = await retry_async(
        asyncio.to_thread,
        index.query,
        vector=query_embedding,
        top_k=top_k,
        namespace=tenant_id,
        filter={"tenant_id": {"$eq": tenant_id}},
        include_metadata=True,
    )

    similar_chunks = []
    for match in results["matches"]:
        similar_chunks.append({
            "score": match["score"],
            "text": match["metadata"].get("text", ""),
            "document_id": match["metadata"].get("document_id"),
            "chunk_id": match["metadata"].get("chunk_id"),
            "sequence": match["metadata"].get("sequence"),
            "page": match["metadata"].get("page"),
        })

    return similar_chunks

async def delete_document_chunks(
    tenant_id: str,
    document_id: str,
) -> None:
    """
    Delete all chunks for a document from Pinecone (within the tenant's
    namespace). Safe to call even if nothing has been indexed yet — Pinecone
    treats deleting a non-matching filter as a no-op, not an error.

    Args:
        tenant_id: Tenant ID — also the Pinecone namespace
        document_id: Document ID to delete
    """
    index = get_index()

    try:
        await retry_async(
            asyncio.to_thread,
            index.delete,
            filter={
                "tenant_id": {"$eq": tenant_id},
                "document_id": {"$eq": document_id},
            },
            namespace=tenant_id,
        )
    except Exception as e:
        # A namespace that doesn't exist yet (nothing indexed for this
        # tenant/document yet) can 404 on some Pinecone client versions —
        # that's not a real failure for a "delete before reindex" call.
        logger.info(f"delete_document_chunks no-op for document {document_id}: {e}")
        return

    logger.info(f"Deleted chunks for document {document_id}")

async def clear_tenant_data(tenant_id: str) -> None:
    """
    Delete all data for a tenant (for GDPR/cleanup) — the tenant's entire
    Pinecone namespace, in one call.

    Args:
        tenant_id: Tenant ID to clear
    """
    index = get_index()

    try:
        await retry_async(asyncio.to_thread, index.delete, delete_all=True, namespace=tenant_id)
    except Exception as e:
        logger.info(f"clear_tenant_data no-op for tenant {tenant_id} (empty namespace?): {e}")
        return

    logger.info(f"Cleared all data for tenant {tenant_id}")
