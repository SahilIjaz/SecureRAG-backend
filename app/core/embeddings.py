"""Embedding service backed by Pinecone Inference (multilingual-e5-large).

Uses Pinecone's hosted embedding model, so no separate embedding-provider key
is needed — the PINECONE_API_KEY covers both embedding and vector storage.
"""

import asyncio
import logging
from collections import OrderedDict
from typing import List

from app.core.retry import retry_async

logger = logging.getLogger(__name__)

# Small in-process LRU cache for QUERY embeddings. The same or repeated
# questions (common in a support chatbot) then skip the Pinecone inference
# round trip entirely, shaving fixed latency off the front of every repeat
# reply. Bounded so memory stays flat; not shared across workers, which is
# fine — it's a best-effort speedup, not a correctness requirement.
_QUERY_EMBED_CACHE_MAX = 512
_query_embed_cache: "OrderedDict[str, List[float]]" = OrderedDict()

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

def _redis_key(text: str) -> str:
    import hashlib

    return "emb:q:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

async def embed_text(text: str) -> List[float]:
    """Embed a search query (input_type='query' — e5 models are asymmetric).

    Two cache layers, both best-effort: a shared Redis cache (when REDIS_URL is
    configured — survives restarts, shared across workers) checked first, then
    an in-process LRU. On a miss both are populated. Any Redis error falls
    straight through to the LRU / Pinecone path, so caching never breaks a
    request."""
    import json

    from app.core.redis_client import get_redis
    from app.config import settings

    key = text.strip()
    redis = get_redis()

    # 1) Shared Redis cache.
    if redis is not None:
        try:
            hit = await redis.get(_redis_key(key))
            if hit:
                return json.loads(hit)
        except Exception:
            pass  # Redis blip → fall through to LRU / Pinecone

    # 2) In-process LRU.
    cached = _query_embed_cache.get(key)
    if cached is not None:
        _query_embed_cache.move_to_end(key)
        return cached

    # 3) Miss — embed via Pinecone and populate both caches.
    embeddings = await _embed_batch([text], "query")
    vector = embeddings[0]

    _query_embed_cache[key] = vector
    _query_embed_cache.move_to_end(key)
    if len(_query_embed_cache) > _QUERY_EMBED_CACHE_MAX:
        _query_embed_cache.popitem(last=False)

    if redis is not None:
        try:
            await redis.set(_redis_key(key), json.dumps(vector), ex=settings.EMBED_CACHE_TTL_SECONDS)
        except Exception:
            pass

    return vector

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
