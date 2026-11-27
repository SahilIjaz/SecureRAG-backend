"""
Computed answer-confidence signals and cross-document conflict detection.

This module gives the pipeline a *mathematical* confidence signal computed
from retrieval evidence — independent of the LLM's self-reported
'CONFIDENCE: NN' line — plus a detector for retrieved chunks that contradict
each other. Three signals, all derived from the retrieval scores and texts
the pipeline already has (no extra model calls, no added latency):

  similarity  — how strong the single best piece of evidence is
                (max of the chunks' relevance scores).
  coverage    — how much of the retrieved set is strong evidence
                (fraction of chunks whose score clears a support floor).
  entropy     — how *spread out* the score distribution is (normalized
                Shannon entropy). Scores concentrated on one chunk → low
                entropy → the evidence agrees on where the answer lives;
                scores spread evenly → high entropy → ambiguity.

  retrieval_confidence = 100 · (0.5·similarity + 0.3·coverage + 0.2·(1 − entropy))

Conflict detection compares the retrieved chunks pairwise for numeric
contradictions — the same quantity described in overlapping words but with
different values ("the price is $10" vs "the price is $15"). Detected
conflicts are *disclosed* in the answer (the prompt instructs the model to
point out the discrepancy and cite both sources) rather than silently
resolved. Tenant-boundary safety is by construction: retrieval is already
scoped to one tenant's Pinecone namespace and Postgres rows, so any two
chunks compared here can only ever belong to the same tenant — a flagged
conflict is a genuine inconsistency in that tenant's own knowledge base,
never a cross-tenant data leak.
"""

from __future__ import annotations

import math
from typing import List

# A chunk whose best available score clears this is counted as "strong
# support" for coverage purposes. Calibrated to the cross-encoder reranker's
# output (rerank_score), whose relevant/irrelevant separation sits far from
# this floor in practice; cosine scores from the pure-vector path cluster
# higher, which only makes coverage more generous there.
_SUPPORT_FLOOR = 0.5

def _chunk_score(chunk: dict) -> float:
    """Best available relevance score for a chunk, clamped to [0, 1].
    Preference order mirrors retrieval quality: cross-encoder rerank score,
    then dense cosine score. Keyword-only chunks carry a ts_rank (tiny,
    unbounded scale) — clamping keeps it from polluting the distribution."""
    for key in ("rerank_score", "score"):
        value = chunk.get(key)
        if value is not None:
            return max(0.0, min(1.0, float(value)))
    return 0.0


def compute_retrieval_metrics(chunks: List[dict]) -> dict:
    """similarity / coverage / entropy / retrieval_confidence for a retrieved
    set. Empty input means nothing was retrieved: all-zero confidence."""
    if not chunks:
        return {
            "similarity": 0.0,
            "coverage": 0.0,
            "entropy": 0.0,
            "retrievalConfidence": 0,
        }

    scores = [_chunk_score(c) for c in chunks]
    similarity = max(scores)
    coverage = sum(1 for s in scores if s >= _SUPPORT_FLOOR) / len(scores)

    # Normalized Shannon entropy of the score distribution. One chunk, or a
    # zero-mass distribution, carries no spread information → entropy 0.
    total = sum(scores)
    if len(scores) < 2 or total <= 0:
        entropy = 0.0
    else:
        probs = [s / total for s in scores if s > 0]
        raw = -sum(p * math.log(p) for p in probs)
        entropy = raw / math.log(len(scores))

    confidence = 100 * (0.5 * similarity + 0.3 * coverage + 0.2 * (1 - entropy))
    return {
        "similarity": round(similarity, 4),
        "coverage": round(coverage, 4),
        "entropy": round(entropy, 4),
        "retrievalConfidence": round(confidence),
    }


def _sigmoid(x: float) -> float:
    # Numerically stable for large |x|.
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def compute_pregen_confidence(chunks: List[dict]) -> dict:
    """Pre-generation confidence from retrieval evidence alone (no model call):

        S = sigmoid(b0 + b1·top_rerank + b2·(top1 - top2) + b3·coverage)

    top_rerank — best chunk's cross-encoder score; gap — how far the best chunk
    stands above the runner-up; coverage — share of chunks clearing the support
    floor. b0..b3 come from settings (RAG_CONF_B0..B3), fitted offline.

    Returns 0-100 `pregenConfidence` so the per-tenant `confidenceThreshold`
    and RAG_CLARIFY_BAND keep their meaning. When no chunk carries a
    rerank_score (reranker disabled, timed out, or failed → RRF order), the
    scale the coefficients were fitted on isn't available, so
    `rerankAvailable` is False and `pregenConfidence` falls back to the legacy
    weighted formula rather than feeding a wrong-scale number to the sigmoid.
    """
    from app.config import settings

    base = compute_retrieval_metrics(chunks)
    if not chunks:
        return {"pregenConfidence": 0, "topRerank": 0.0, "gap": 0.0,
                "coverage": 0.0, "rerankAvailable": False}

    rerank_available = any(c.get("rerank_score") is not None for c in chunks)
    scores = sorted((_chunk_score(c) for c in chunks), reverse=True)
    top_rerank = scores[0]
    gap = scores[0] - scores[1] if len(scores) > 1 else 0.0
    coverage = base["coverage"]

    if settings.RAG_PREGEN_CONF_ENABLED and rerank_available:
        z = (settings.RAG_CONF_B0 + settings.RAG_CONF_B1 * top_rerank
             + settings.RAG_CONF_B2 * gap + settings.RAG_CONF_B3 * coverage)
        value = round(100 * _sigmoid(z))
    else:
        value = base["retrievalConfidence"]

    return {
        "pregenConfidence": int(value),
        "topRerank": round(top_rerank, 4),
        "gap": round(gap, 4),
        "coverage": round(coverage, 4),
        "rerankAvailable": rerank_available,
    }


# Conflict detection and prompt rendering moved to
# app/services/conflict_detection.py (numeric + date + semantic detectors);
# resolution lives in app/services/conflict_resolution.py. Re-exported here
# so existing imports and tests keep working.
from app.services.conflict_detection import (  # noqa: E402,F401
    conflict_prompt_note,
    detect_conflicts,
    verify_semantic_conflicts,
)
