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

# Cap on how many query words the OR-form tsquery keeps — bounds the query
# cost for a pathological pasted-wall-of-text question.
_KEYWORD_MAX_TERMS = 12

# Lazily-built AUTOCOMMIT view of the app engine (shares its connection pool;
# see _keyword_search for why autocommit). Built on first use rather than at
# import so this module never forces engine creation just by being imported.
_ac_engine = None


def _autocommit_engine():
    global _ac_engine
    if _ac_engine is None:
        from app.database import async_engine

        _ac_engine = async_engine.execution_options(isolation_level="AUTOCOMMIT")
    return _ac_engine


def _chunk_key(chunk: dict) -> str:
    """Stable identity for de-duplicating the same chunk across both rankings."""
    cid = chunk.get("chunk_id")
    if cid:
        return f"{chunk.get('document_id')}#{cid}"
    return f"{chunk.get('document_id')}#{chunk.get('sequence')}"


async def _vector_search(
    query: str, tenant_id: str, top_k: int, document_ids: list[str] | None
) -> list[dict]:
    """Dense search via Pinecone. Applies a minimum-score threshold so weak,
    off-topic matches don't get dragged into the context just to fill top_k."""
    if not settings.PINECONE_API_KEY:
        return []
    from app.core.vector_store import search_chunks

    embedding = await embed_text(query)
    chunks = await search_chunks(embedding, tenant_id, top_k=top_k, document_ids=document_ids)
    threshold = settings.RAG_MIN_VECTOR_SCORE
    kept = [c for c in chunks if float(c.get("score", 0)) >= threshold]
    return kept


async def _keyword_search(
    query: str, tenant_id: str, top_k: int, document_ids: list[str] | None = None
) -> list[dict]:
    """Lexical search over the Postgres tsvector column. Uses websearch_to_tsquery
    so a plain user string ("returns policy refund") is parsed safely — no risk
    of a tsquery syntax error from arbitrary input.

    Runs on an AUTOCOMMIT connection rather than a session: this is a single
    read-only SELECT on the reply hot path, and the session pattern costs three
    extra network round trips (BEGIN + query + ROLLBACK, plus the pool's
    pre-ping) — measured at ~790ms against Neon for a query whose server-side
    execution is under 1ms. Autocommit sends just the query itself."""
    import uuid as uuid_mod

    # OR the query terms instead of websearch's default AND. AND semantics
    # made conversational queries return nothing: "tell me about your refund
    # policy" required tell & refund & policy to all appear in ONE chunk
    # (me/about/your are stopwords, but "tell" isn't). With OR, ts_rank still
    # scores chunks matching MORE terms higher, so exact-token lookups keep
    # winning while partial matches finally contribute to the RRF fusion.
    or_query = " or ".join(query.split()[:_KEYWORD_MAX_TERMS]) or query
    tsquery = func.websearch_to_tsquery("english", or_query)
    rank = func.ts_rank(DocumentChunk.ts, tsquery)
    where = [
        DocumentChunk.tenant_id == tenant_id,
        DocumentChunk.ts.op("@@")(tsquery),
    ]
    if document_ids is not None:
        where.append(
            DocumentChunk.document_id.in_([uuid_mod.UUID(str(d)) for d in document_ids])
        )
    stmt = (
        select(
            DocumentChunk.document_id,
            DocumentChunk.chunk_id,
            DocumentChunk.sequence,
            DocumentChunk.text,
            rank.label("rank"),
        )
        .where(*where)
        .order_by(rank.desc())
        .limit(top_k)
    )
    async with _autocommit_engine().connect() as conn:
        rows = (await conn.execute(stmt)).all()
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
    document_ids: list[str] | None = None,
) -> list[dict]:
    """Run vector and keyword search concurrently and fuse the results.

    document_ids, when given, pre-filters both sides to those documents (the
    caller's active set) — see search_chunks for why pre-filtering beats
    dropping stale chunks after retrieval.

    Falls back gracefully: if one side errors or returns nothing, the other
    still produces results. Returns [] only when both find nothing relevant —
    callers treat that as "no context" and use the tenant's fallback message.
    """
    k = top_k or settings.RAG_SEARCH_TOP_K
    # Pull a few extra from each side so fusion has room to reorder — and a
    # genuinely wide pool when the rerank stage will narrow it back down.
    per_source_k = max(k * 2, k + 3)
    if settings.RAG_RERANK_ENABLED:
        per_source_k = max(per_source_k, settings.RAG_RERANK_CANDIDATES)

    vector_task = _vector_search(query, tenant_id, per_source_k, document_ids)
    keyword_task = _keyword_search(query, tenant_id, per_source_k, document_ids)
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
        fused = rankings[0][:per_source_k]
    else:
        fused = _reciprocal_rank_fusion(rankings, per_source_k)

    if settings.RAG_RERANK_ENABLED and len(fused) > k:
        return await _rerank(query, fused, k)
    return fused[:k]


async def _rerank(query: str, chunks: list[dict], top_k: int) -> list[dict]:
    """Stage two of retrieve-then-rerank: score each candidate chunk against
    the query with Pinecone's hosted cross-encoder and keep the best top_k.
    RRF fusion orders by *rank agreement* between the two searches; the
    reranker actually reads the text, so it separates "mentions the words"
    from "answers the question" — the difference grows with corpus size.

    Best-effort by contract: any failure or timeout returns the fused order
    unchanged, so reranking can never break or block a reply."""
    try:
        from app.core.vector_store import _get_pinecone

        def _blocking():
            pc = _get_pinecone()
            return pc.inference.rerank(
                model=settings.RAG_RERANK_MODEL,
                query=query,
                # The model reads ~1k tokens per document; cap the text so an
                # 8000-char chunk doesn't get rejected outright.
                documents=[(c.get("text") or "")[:4000] for c in chunks],
                top_n=top_k,
                return_documents=False,
                parameters={"truncate": "END"},
            )

        result = await asyncio.wait_for(asyncio.to_thread(_blocking), timeout=8)
        reranked = []
        for row in result.data:
            chunk = chunks[row.index]
            chunk["rerank_score"] = round(float(row.score), 6)
            reranked.append(chunk)
        return reranked
    except Exception as e:
        logger.warning("Rerank failed (%s) — using fused order", e)
        return chunks[:top_k]
