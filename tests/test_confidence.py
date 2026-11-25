"""
Tests for the computed-confidence signals, cross-document conflict detection
(app/services/confidence.py) and the central gate decision in rag_service.

Run with:  PYTHONPATH=. ./venv/bin/pytest tests/ -v
"""

import pytest

from app.config import settings
from app.services.confidence import (
    compute_pregen_confidence,
    compute_retrieval_metrics,
    conflict_prompt_note,
    detect_conflicts,
)
from app.services.rag_service import _gate_decision, _pregen_confidence, _pregen_gate


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


def test_same_document_not_flagged_in_cross_document_mode():
    chunks = [
        _chunk("doc-a", "The plan price is 10 dollars per month."),
        _chunk("doc-a", "The plan price is 15 dollars per month."),
    ]
    assert detect_conflicts(chunks, intra_document=False) == []


def test_same_document_flagged_as_intra_document_scope():
    chunks = [
        _chunk("doc-a", "The plan price is 10 dollars per month."),
        _chunk("doc-a", "The plan price is 15 dollars per month."),
    ]
    conflicts = detect_conflicts(chunks, intra_document=True)
    assert len(conflicts) == 1
    assert conflicts[0]["scope"] == "intra_document"


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


def test_pregen_confidence_prefers_sigmoid_then_legacy():
    assert _pregen_confidence({"pregenConfidence": 80, "retrievalConfidence": 20}) == 80
    assert _pregen_confidence({"retrievalConfidence": 42}) == 42
    assert _pregen_confidence({}) is None
    # None → "answer": without any signal the gate cannot judge.
    assert _gate_decision(None, 60, handoff_enabled=True) == "answer"


# ── Pre-generation confidence (sigmoid over rerank / gap / coverage) ─────────

def test_pregen_empty_is_zero():
    m = compute_pregen_confidence([])
    assert m["pregenConfidence"] == 0 and m["rerankAvailable"] is False


def test_pregen_monotonic_in_rerank_score():
    low = compute_pregen_confidence([{"rerank_score": 0.2}, {"rerank_score": 0.1}])
    mid = compute_pregen_confidence([{"rerank_score": 0.6}, {"rerank_score": 0.1}])
    high = compute_pregen_confidence([{"rerank_score": 0.95}, {"rerank_score": 0.1}])
    assert low["pregenConfidence"] < mid["pregenConfidence"] < high["pregenConfidence"]
    assert high["pregenConfidence"] > 80 and low["pregenConfidence"] < 20


def test_pregen_single_chunk_gap_is_zero():
    m = compute_pregen_confidence([{"rerank_score": 0.9}])
    assert m["gap"] == 0.0 and m["topRerank"] == 0.9


def test_pregen_gap_is_top1_minus_top2():
    m = compute_pregen_confidence([{"rerank_score": 0.3}, {"rerank_score": 0.9}])
    assert m["topRerank"] == 0.9 and m["gap"] == 0.6


def test_pregen_without_rerank_falls_back_to_legacy_formula():
    chunks = [{"score": 0.8}, {"score": 0.7}]
    m = compute_pregen_confidence(chunks)
    assert m["rerankAvailable"] is False
    assert m["pregenConfidence"] == compute_retrieval_metrics(chunks)["retrievalConfidence"]


def test_pregen_disabled_flag_uses_legacy_formula():
    chunks = [{"rerank_score": 0.9}, {"rerank_score": 0.1}]
    saved = settings.RAG_PREGEN_CONF_ENABLED
    settings.RAG_PREGEN_CONF_ENABLED = False
    try:
        m = compute_pregen_confidence(chunks)
        assert m["pregenConfidence"] == compute_retrieval_metrics(chunks)["retrievalConfidence"]
    finally:
        settings.RAG_PREGEN_CONF_ENABLED = saved


def test_pregen_gate_refuses_weak_evidence():
    weak = {"pregenConfidence": 10}
    assert _pregen_gate(weak, "what is the refund policy", None, 60, False) == ("refuse", 10)
    assert _pregen_gate(weak, "what is the refund policy", None, 60, True) == ("handoff", 10)


def test_pregen_gate_relaxes_short_followup():
    weak = {"pregenConfidence": 10}
    history = [{"role": "user", "text": "hi"}, {"role": "bot", "text": "hello"}]
    # Short message + history → never refused.
    assert _pregen_gate(weak, "yes please", history, 60, False)[0] == "answer"
    # A long standalone question with history is still gated.
    long_q = "what is the full refund policy for annual plans please"
    assert _pregen_gate(weak, long_q, history, 60, False)[0] == "refuse"


@pytest.mark.asyncio
async def test_weak_evidence_refused_without_llm_call(monkeypatch):
    """The whole point: a refusal must not spend an LLM generation."""
    from app.services import rag_service

    chunks = [{"rerank_score": 0.05, "text": "unrelated", "document_id": "d1"}]
    metrics = {**compute_retrieval_metrics(chunks), **compute_pregen_confidence(chunks), "conflicts": []}

    async def fake_prepare(*a, **k):
        return chunks, "PROMPT", 200, False, metrics

    async def boom(*a, **k):
        raise AssertionError("LLM must not be called when the pre-gate refuses")

    monkeypatch.setattr(rag_service, "_prepare_generation", fake_prepare)
    monkeypatch.setattr(rag_service.llm_router, "generate", boom)

    config = {"identity": {"fallbackMessage": "FALLBACK"}, "behavior": {"confidenceThreshold": 60}}
    result = await rag_service.answer_question("t1", "completely unrelated question here", config=config, doc_names={"d1": "a.pdf"})
    assert result["answer"] == "FALLBACK" and result["refused"] is True and result["sources"] == []
