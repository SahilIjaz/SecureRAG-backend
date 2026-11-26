"""Live Pinecone tenant-isolation test (TC_SEC) — opt-in.

Runs only when ``RUN_LIVE_PINECONE=1`` and a Pinecone key/index are configured,
because it writes to the real index. It uses two throw-away tenant ids (random
UUIDs, so two throw-away namespaces) and a deterministic random vector in place
of a real embedding — a stronger test than a semantic one, since querying B's
namespace with A's *exact* vector would be an identical match if namespaces
leaked. Everything it writes is deleted afterwards.

Run:  RUN_LIVE_PINECONE=1 pytest tests/test_pinecone_isolation_live.py -v
"""
import os
import random
import uuid

import pytest

from app.config import settings
from app.core import vector_store
from app.core.embeddings import EMBEDDING_DIMENSION

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_PINECONE") != "1" or not settings.PINECONE_API_KEY,
    reason="live Pinecone test: set RUN_LIVE_PINECONE=1 (writes to the configured index)",
)

CODEWORD_A = "ZEPHYR-ALPHA-7731"
CODEWORD_B = "QUASAR-BRAVO-9954"


def _vec(seed: int):
    rnd = random.Random(seed)
    return [rnd.uniform(-1, 1) for _ in range(EMBEDDING_DIMENSION)]


def _chunk(codeword: str):
    return {"chunk_id": "c0", "sequence": 0, "text": f"Internal secret code is {codeword}.", "token_count": 8}


@pytest.fixture
async def two_namespaces():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    doc_a, doc_b = str(uuid.uuid4()), str(uuid.uuid4())
    vec_a, vec_b = _vec(1), _vec(2)
    await vector_store.upsert_chunks(a, doc_a, [_chunk(CODEWORD_A)], [vec_a])
    await vector_store.upsert_chunks(b, doc_b, [_chunk(CODEWORD_B)], [vec_b])
    # Pinecone serverless is eventually consistent — wait until both are visible.
    import asyncio
    for _ in range(30):
        ra = await vector_store.search_chunks(vec_a, a, top_k=3)
        rb = await vector_store.search_chunks(vec_b, b, top_k=3)
        if ra and rb:
            break
        await asyncio.sleep(2)
    yield {"a": a, "b": b, "doc_a": doc_a, "doc_b": doc_b, "vec_a": vec_a, "vec_b": vec_b}
    await vector_store.clear_tenant_data(a)
    await vector_store.clear_tenant_data(b)


async def test_tenant_retrieves_its_own_vectors(two_namespaces):
    t = two_namespaces
    hits = await vector_store.search_chunks(t["vec_a"], t["a"], top_k=5)
    assert any(CODEWORD_A in h["text"] for h in hits)
    assert all(h["document_id"] == t["doc_a"] for h in hits)


async def test_identical_vector_in_other_tenant_returns_nothing_of_a(two_namespaces):
    """B searches with A's exact vector: an identical match if namespaces leaked."""
    t = two_namespaces
    hits = await vector_store.search_chunks(t["vec_a"], t["b"], top_k=10)
    assert all(CODEWORD_A not in h["text"] for h in hits)
    assert all(h["document_id"] != t["doc_a"] for h in hits)
    assert all(h["document_id"] == t["doc_b"] for h in hits)


async def test_document_id_filter_cannot_reach_across_tenants(two_namespaces):
    """Even naming tenant A's document id explicitly, B's search finds nothing."""
    t = two_namespaces
    hits = await vector_store.search_chunks(t["vec_a"], t["b"], top_k=10, document_ids=[t["doc_a"]])
    assert hits == []


async def test_raw_namespace_query_is_empty_for_foreign_tenant_filter(two_namespaces):
    """Bypass the helper: query B's namespace with a metadata filter for tenant A."""
    import asyncio
    t = two_namespaces
    index = vector_store.get_index()
    res = await asyncio.to_thread(
        index.query, vector=t["vec_a"], top_k=10, namespace=t["b"],
        filter={"tenant_id": {"$eq": t["a"]}}, include_metadata=True,
    )
    assert res["matches"] == []


async def test_deleting_tenant_a_leaves_tenant_b_intact(two_namespaces):
    import asyncio
    t = two_namespaces
    await vector_store.clear_tenant_data(t["a"])
    await asyncio.sleep(3)
    gone = await vector_store.search_chunks(t["vec_a"], t["a"], top_k=5)
    kept = await vector_store.search_chunks(t["vec_b"], t["b"], top_k=5)
    assert gone == []
    assert any(CODEWORD_B in h["text"] for h in kept)
