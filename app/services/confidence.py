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
import re
from typing import List, Optional

# A chunk whose best available score clears this is counted as "strong
# support" for coverage purposes. Calibrated to the cross-encoder reranker's
# output (rerank_score), whose relevant/irrelevant separation sits far from
# this floor in practice; cosine scores from the pure-vector path cluster
# higher, which only makes coverage more generous there.
_SUPPORT_FLOOR = 0.5

# Conflict detection knobs: how many alphabetic context words to take around
# each number, how many of them two mentions must share to be judged "the
# same fact", and the minimum relative difference for two values to count as
# disagreeing (filters float noise like 10 vs 10.0).
_CONTEXT_WINDOW = 4
_MIN_SHARED_CONTEXT = 2
_MIN_RELATIVE_DIFF = 0.01
_MAX_CONFLICTS_REPORTED = 3

_NUMBER_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?![\w.])")
_WORD_RE = re.compile(r"[a-zA-Z]{3,}")

_STOPWORDS = frozenset(
    "the and for are was were with from that this have has not you your our "
    "their its all any can will each per more than then when where which".split()
)


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


def _numeric_facts(text: str) -> List[dict]:
    """Extract (value, context-word set) pairs for every number in `text`."""
    facts = []
    for match in _NUMBER_RE.finditer(text):
        value = float(match.group(1).replace(",", ""))
        before = text[: match.start()]
        after = text[match.end() :]
        context = [w.lower() for w in _WORD_RE.findall(before)[-_CONTEXT_WINDOW:]]
        context += [w.lower() for w in _WORD_RE.findall(after)[:_CONTEXT_WINDOW]]
        keywords = {w for w in context if w not in _STOPWORDS}
        if keywords:
            facts.append({"value": value, "context": keywords, "snippet_at": match.start()})
    return facts


def _values_disagree(a: float, b: float) -> bool:
    baseline = max(abs(a), abs(b))
    if baseline == 0:
        return False
    return abs(a - b) / baseline >= _MIN_RELATIVE_DIFF


def detect_conflicts(chunks: List[dict]) -> List[dict]:
    """Pairwise numeric-contradiction scan across chunks from *different*
    documents (two chunks of one document sharing a number is normal overlap,
    not a conflict). Returns up to _MAX_CONFLICTS_REPORTED entries:
        {"documentA", "documentB", "valueA", "valueB", "context"}"""
    per_chunk = [
        (str(c.get("document_id")), _numeric_facts(c.get("text") or ""))
        for c in chunks
    ]
    conflicts: List[dict] = []
    seen_pairs = set()

    for i in range(len(per_chunk)):
        doc_a, facts_a = per_chunk[i]
        for j in range(i + 1, len(per_chunk)):
            doc_b, facts_b = per_chunk[j]
            if doc_a == doc_b:
                continue
            for fa in facts_a:
                for fb in facts_b:
                    shared = fa["context"] & fb["context"]
                    if len(shared) < _MIN_SHARED_CONTEXT:
                        continue
                    if not _values_disagree(fa["value"], fb["value"]):
                        continue
                    dedup = (doc_a, doc_b, frozenset(shared))
                    if dedup in seen_pairs:
                        continue
                    seen_pairs.add(dedup)
                    conflicts.append({
                        "documentA": doc_a,
                        "documentB": doc_b,
                        "valueA": fa["value"],
                        "valueB": fb["value"],
                        "context": " ".join(sorted(shared)),
                    })
                    if len(conflicts) >= _MAX_CONFLICTS_REPORTED:
                        return conflicts
    return conflicts


def conflict_prompt_note(conflicts: List[dict], doc_names: Optional[dict]) -> str:
    """Render detected conflicts as a STYLE-RULES instruction (never placed
    inside the untrusted CONTEXT block). Empty string when no conflicts."""
    if not conflicts:
        return ""
    lines = []
    for c in conflicts:
        name_a = (doc_names or {}).get(c["documentA"]) or "one source"
        name_b = (doc_names or {}).get(c["documentB"]) or "another source"
        lines.append(
            f"{name_a} says {c['valueA']:g} but {name_b} says {c['valueB']:g} (about: {c['context']})"
        )
    listed = "; ".join(lines)
    return (
        "The retrieved sources CONTRADICT each other on specific figures: "
        f"{listed}. Do not silently pick one — briefly point out that the "
        "sources disagree, present both values with their source names, and "
        "suggest confirming the current one."
    )
