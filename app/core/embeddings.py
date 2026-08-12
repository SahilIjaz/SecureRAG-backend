"""Embedding service backed by Pinecone Inference (multilingual-e5-large).

Uses Pinecone's hosted embedding model, so no separate embedding-provider key
is needed — the PINECONE_API_KEY covers both embedding and vector storage.
"""

import asyncio
import logging
from typing import List

from app.core.retry import retry_async

logger = logging.getLogger(__name__)

# multilingual-e5-large produces 1024-dim vectors; the Pinecone index must match.
EMBEDDING_MODEL = "multilingual-e5-large"
EMBEDDING_DIMENSION = 1024

# Pinecone inference accepts at most 96 inputs per request.
_BATCH_SIZE = 96
# Cap on concurrent in-flight embed batches — parallelizes large documents
# without hammering Pinecone's rate limits.
_MAX_CONCURRENT_BATCHES = 4

def _embed_batch_blocking(texts: List[str], input_type: str) -> List[List[float]]:
    from app.core.vector_store import _get_pinecone

    pc = _get_pinecone()
    result = pc.inference.embed(
        model=EMBEDDING_MODEL,
        inputs=texts,
        parameters={"input_type": input_type, "truncate": "END"},
    )
    return [item["values"] for item in result.data]

async def _embed_batch(texts: List[str], input_type: str) -> List[List[float]]:
    return await retry_async(asyncio.to_thread, _embed_batch_blocking, texts, input_type)

async def embed_text(text: str) -> List[float]:
    """Embed a search query (input_type='query' — e5 models are asymmetric)."""
    embeddings = await _embed_batch([text], "query")
    return embeddings[0]

async def embed_chunks(texts: List[str]) -> List[List[float]]:
    """Embed document chunks for indexing (input_type='passage'), batched and
    run concurrently (bounded by _MAX_CONCURRENT_BATCHES) rather than one
    batch at a time — matters for large documents with many chunks."""
    batches = [texts[i : i + _BATCH_SIZE] for i in range(0, len(texts), _BATCH_SIZE)]
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_BATCHES)

    async def _bounded(batch: List[str]) -> List[List[float]]:
        async with semaphore:
            return await _embed_batch(batch, "passage")

    results = await asyncio.gather(*(_bounded(batch) for batch in batches))
    embeddings = [vec for batch_result in results for vec in batch_result]
    logger.info("Generated embeddings for %d chunks", len(texts))
    return embeddings
