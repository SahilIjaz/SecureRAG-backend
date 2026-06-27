"""Pinecone vector store for document chunks."""

import asyncio
import logging
import uuid
from typing import List, Dict, Any
from pinecone import Pinecone

from app.config import settings

logger = logging.getLogger(__name__)

_pc = None


def _get_pinecone():
    """Lazy-load Pinecone client."""
    global _pc
    if _pc is None:
        _pc = Pinecone(api_key=settings.PINECONE_API_KEY)
    return _pc


def get_index(index_name: str = "securerag-documents"):
    """Get Pinecone index."""
    pc = _get_pinecone()
    return pc.Index(index_name)


async def upsert_chunks(
    tenant_id: str,
    document_id: str,
    chunks: List[Dict[str, Any]],
    embeddings: List[List[float]],
) -> None:
    """
    Upsert document chunks to Pinecone.

    Args:
        tenant_id: Tenant ID for multi-tenancy
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
            "text": chunk["text"][:200],
            "token_count": chunk["token_count"],
        }

        vectors_to_upsert.append((vector_id, embedding, metadata))

    batch_size = 100
    for i in range(0, len(vectors_to_upsert), batch_size):
        batch = vectors_to_upsert[i:i+batch_size]
        await asyncio.to_thread(index.upsert, vectors=batch)
        logger.info(f"Upserted batch {i//batch_size + 1} of chunks")

    logger.info(f"Upserted {len(vectors_to_upsert)} chunks for document {document_id}")


async def search_chunks(
    query_embedding: List[float],
    tenant_id: str,
    top_k: int = 5,
) -> List[Dict[str, Any]]:
    """
    Search for similar chunks using vector similarity.

    Args:
        query_embedding: Embedding of the query
        tenant_id: Tenant ID for filtering
        top_k: Number of top results to return

    Returns:
        List of similar chunks with metadata
    """
    index = get_index()

    results = await asyncio.to_thread(
        index.query,
        vector=query_embedding,
        top_k=top_k,
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
        })

    return similar_chunks


async def delete_document_chunks(
    tenant_id: str,
    document_id: str,
) -> None:
    """
    Delete all chunks for a document from Pinecone.

    Args:
        tenant_id: Tenant ID
        document_id: Document ID to delete
    """
    index = get_index()

    await asyncio.to_thread(
        index.delete,
        filter={
            "tenant_id": {"$eq": tenant_id},
            "document_id": {"$eq": document_id},
        }
    )

    logger.info(f"Deleted chunks for document {document_id}")


async def clear_tenant_data(tenant_id: str) -> None:
    """
    Delete all data for a tenant (for GDPR/cleanup).

    Args:
        tenant_id: Tenant ID to clear
    """
    index = get_index()

    await asyncio.to_thread(
        index.delete,
        filter={"tenant_id": {"$eq": tenant_id}}
    )

    logger.info(f"Cleared all data for tenant {tenant_id}")

