"""
Tests for the computed-confidence signals, cross-document conflict detection
(app/services/confidence.py) and the central gate decision in rag_service.

Run with:  PYTHONPATH=. ./venv/bin/pytest tests/ -v
"""

from app.config import settings
from app.services.confidence import (
    compute_retrieval_metrics,
    conflict_prompt_note,
    detect_conflicts,
)
from app.services.rag_service import _effective_confidence, _gate_decision


# ── Retrieval metrics (similarity / coverage / entropy formula) ──────────────

def test_empty_retrieval_is_zero_confidence():
    m = compute_retrieval_metrics([])
    assert m["retrievalConfidence"] == 0
    assert m["similarity"] == 0.0 and m["coverage"] == 0.0


def test_strong_concentrated_evidence_scores_high():
    chunks = [{"rerank_score": 0.98}, {"rerank_score": 0.1}, {"rerank_score": 0.05}]
    m = compute_retrieval_metrics(chunks)
    assert m["similarity"] == 0.98
    assert 0 < m["entropy"] < 1  # concentrated, but not single-chunk
    assert m["retrievalConfidence"] >= 60


def test_weak_evidence_scores_low():
    chunks = [{"score": 0.2}, {"score": 0.18}, {"score": 0.21}]
    m = compute_retrieval_metrics(chunks)
    assert m["coverage"] == 0.0  # nothing clears the support floor
    assert m["entropy"] > 0.9  # evenly spread = maximal ambiguity
    assert m["retrievalConfidence"] < 40


def test_rerank_score_preferred_over_cosine():
    m = compute_retrieval_metrics([{"rerank_score": 0.9, "score": 0.1}])
    assert m["similarity"] == 0.9


def test_single_chunk_has_zero_entropy():
    assert compute_retrieval_metrics([{"score": 0.8}])["entropy"] == 0.0


# ── Conflict detection ───────────────────────────────────────────────────────

def _chunk(doc, text):
    return {"document_id": doc, "text": text}


def test_price_conflict_across_documents_detected():
    chunks = [
        _chunk("doc-a", "The premium plan price is $10 per month for all users."),
        _chunk("doc-b", "Our premium plan price is 15 per month, billed yearly."),
    ]
    conflicts = detect_conflicts(chunks)
    assert len(conflicts) == 1
    c = conflicts[0]
    assert {c["valueA"], c["valueB"]} == {10.0, 15.0}
    assert "premium" in c["context"] or "price" in c["context"]


def test_same_document_never_conflicts():
    chunks = [
        _chunk("doc-a", "The plan price is 10 dollars per month."),
        _chunk("doc-a", "The plan price is 15 dollars per month."),
    ]
    assert detect_conflicts(chunks) == []


def test_unrelated_numbers_do_not_conflict():
    chunks = [
        _chunk("doc-a", "Our office opened in 2019 near the station."),
        _chunk("doc-b", "Delivery takes 5 business days on average."),
    ]
    assert detect_conflicts(chunks) == []


def test_identical_values_do_not_conflict():
    chunks = [
        _chunk("doc-a", "The plan price is 10 per month."),
        _chunk("doc-b", "Yes — the plan price is 10 per month."),
    ]
    assert detect_conflicts(chunks) == []


def test_conflict_note_renders_document_names():
    chunks = [
        _chunk("doc-a", "Standard shipping cost is 10 dollars nationwide."),
        _chunk("doc-b", "Standard shipping cost is 25 dollars nationwide."),
    ]
    conflicts = detect_conflicts(chunks)
    note = conflict_prompt_note(conflicts, {"doc-a": "Pricing.pdf", "doc-b": "FAQ.md"})
    assert "Pricing.pdf" in note and "FAQ.md" in note
    assert "disagree" in note
    assert conflict_prompt_note([], {}) == ""


# ── Gate decision (answer / clarify / handoff / refuse) ─────────────────────

def test_gate_low_confidence_with_handoff_hands_off():
    assert _gate_decision(30, 60, handoff_enabled=True) == "handoff"


def test_gate_low_confidence_without_handoff_refuses():
    # The previously-missing case: handoff OFF no longer ships weak answers.
    assert _gate_decision(30, 60, handoff_enabled=False) == "refuse"


def test_gate_borderline_clarifies():
    saved = settings.RAG_CLARIFY_ENABLED
    settings.RAG_CLARIFY_ENABLED = True
    try:
        assert _gate_decision(60, 60, handoff_enabled=True) == "clarify"
        assert _gate_decision(60 + settings.RAG_CLARIFY_BAND - 1, 60, False) == "clarify"
        assert _gate_decision(60 + settings.RAG_CLARIFY_BAND, 60, False) == "answer"
    finally:
        settings.RAG_CLARIFY_ENABLED = saved


def test_gate_clarify_disabled_answers():
    saved = settings.RAG_CLARIFY_ENABLED
    settings.RAG_CLARIFY_ENABLED = False
    try:
        assert _gate_decision(60, 60, handoff_enabled=True) == "answer"
    finally:
        settings.RAG_CLARIFY_ENABLED = saved


def test_gate_high_confidence_answers():
    assert _gate_decision(95, 60, handoff_enabled=True) == "answer"


def test_effective_confidence_prefers_self_report_then_formula():
    assert _effective_confidence(80, {"retrievalConfidence": 20}) == 80
    assert _effective_confidence(None, {"retrievalConfidence": 42}) == 42
    assert _effective_confidence(None, {}) is None
    # None → "answer": without any signal the gate cannot judge.
    assert _gate_decision(None, 60, handoff_enabled=True) == "answer"
