"""
Hybrid retrieval: dense vector search (Pinecone) fused with lexical keyword
search (Postgres full-text) via Reciprocal Rank Fusion.

Why: pure dense search blurs exact tokens — product codes, names, error
strings — so a question mentioning "SKU-4417" may not surface the chunk that
contains it verbatim. Keyword search nails those; vector search nails
paraphrase/semantic matches. RRF combines the two rankings without needing the
scores to be on the same scale.

Tenant isolation matches the rest of the stack: the vector search is scoped to
the tenant's Pinecone namespace, the keyword search filters on tenant_id.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import func, select

from app.config import settings
from app.core.embeddings import embed_text
from app.models.document_chunk import DocumentChunk

logger = logging.getLogger(__name__)

# RRF constant — the standard k=60 from the original RRF paper. Larger k
# flattens the contribution of rank position; 60 is a well-tested default.
_RRF_K = 60


def _chunk_key(chunk: dict) -> str:
    """Stable identity for de-duplicating the same chunk across both rankings."""
    cid = chunk.get("chunk_id")
    if cid:
        return f"{chunk.get('document_id')}#{cid}"
    return f"{chunk.get('document_id')}#{chunk.get('sequence')}"


async def _vector_search(query: str, tenant_id: str, top_k: int) -> list[dict]:
    """Dense search via Pinecone. Applies a minimum-score threshold so weak,
    off-topic matches don't get dragged into the context just to fill top_k."""
    if not settings.PINECONE_API_KEY:
        return []
    from app.core.vector_store import search_chunks

    embedding = await embed_text(query)
    chunks = await search_chunks(embedding, tenant_id, top_k=top_k)
    threshold = settings.RAG_MIN_VECTOR_SCORE
    kept = [c for c in chunks if float(c.get("score", 0)) >= threshold]
    return kept


async def _keyword_search(query: str, tenant_id: str, top_k: int) -> list[dict]:
    """Lexical search over the Postgres tsvector column. Uses websearch_to_tsquery
    so a plain user string ("returns policy refund") is parsed safely — no risk
    of a tsquery syntax error from arbitrary input. Opens its own short-lived
    session since the RAG entry points don't carry one."""
    from app.database import AsyncSessionLocal

    tsquery = func.websearch_to_tsquery("english", query)
    rank = func.ts_rank(DocumentChunk.ts, tsquery)
    stmt = (
        select(
            DocumentChunk.document_id,
            DocumentChunk.chunk_id,
            DocumentChunk.sequence,
            DocumentChunk.text,
            rank.label("rank"),
        )
        .where(
            DocumentChunk.tenant_id == tenant_id,
            DocumentChunk.ts.op("@@")(tsquery),
        )
        .order_by(rank.desc())
        .limit(top_k)
    )
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(stmt)).all()
    return [
        {
            "document_id": str(r.document_id),
            "chunk_id": r.chunk_id,
            "sequence": r.sequence,
            "text": r.text,
            "score": float(r.rank),
        }
        for r in rows
    ]


def _reciprocal_rank_fusion(
    rankings: list[list[dict]], top_k: int
) -> list[dict]:
    """Merge several ranked lists into one. Each chunk's fused score is the sum
    over lists of 1/(k + rank_in_that_list); chunks appearing high in multiple
    lists rise to the top."""
    fused: dict[str, dict] = {}
    scores: dict[str, float] = {}
    for ranking in rankings:
        for position, chunk in enumerate(ranking):
            key = _chunk_key(chunk)
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + position + 1)
            # Keep the first-seen copy of the chunk (they carry the same text).
            fused.setdefault(key, chunk)
    ordered = sorted(fused.values(), key=lambda c: scores[_chunk_key(c)], reverse=True)
    for c in ordered:
        c["fused_score"] = round(scores[_chunk_key(c)], 6)
    return ordered[:top_k]


async def hybrid_search(
    query: str,
    tenant_id: str,
    top_k: int | None = None,
) -> list[dict]:
    """Run vector and keyword search concurrently and fuse the results.

    Falls back gracefully: if one side errors or returns nothing, the other
    still produces results. Returns [] only when both find nothing relevant —
    callers treat that as "no context" and use the tenant's fallback message.
    """
    k = top_k or settings.RAG_SEARCH_TOP_K
    # Pull a few extra from each side so fusion has room to reorder.
    per_source_k = max(k * 2, k + 3)

    vector_task = _vector_search(query, tenant_id, per_source_k)
    keyword_task = _keyword_search(query, tenant_id, per_source_k)
    vector_res, keyword_res = await asyncio.gather(
        vector_task, keyword_task, return_exceptions=True
    )

    rankings: list[list[dict]] = []
    if isinstance(vector_res, Exception):
        logger.warning("Vector search failed for tenant %s: %s", tenant_id, vector_res)
    elif vector_res:
        rankings.append(vector_res)
    if isinstance(keyword_res, Exception):
        logger.warning("Keyword search failed for tenant %s: %s", tenant_id, keyword_res)
    elif keyword_res:
        rankings.append(keyword_res)

    if not rankings:
        return []
    if len(rankings) == 1:
        return rankings[0][:k]
    return _reciprocal_rank_fusion(rankings, k)
