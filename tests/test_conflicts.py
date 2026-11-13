"""
Contradiction detection, evidence-based resolution and pipeline integration
(app/services/conflict_detection.py, conflict_resolution.py,
document_authority_service.py, rag_service.py helpers).

Covers the nine required cases:
  1 numeric contradictions            6 missing authority → unresolved
  2 semantic contradictions           7 cross-tenant isolation (unit + DB)
  3 conflicting conditions and dates  8 unresolved → correct fallback
  4 approved newer supersedes older   9 no conflict → pipeline unchanged
  5 newer-but-unapproved never overrides approved

Run with:  PYTHONPATH=. ./venv/bin/pytest tests/test_conflicts.py -v
The two DB-backed tests skip themselves when the database is unreachable.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from pydantic import ValidationError

from app.config import settings
from app.schemas import frontend as fe
from app.services import rag_service
from app.services.conflict_detection import conflict_prompt_note, detect_conflicts
from app.services.conflict_resolution import resolve_conflict, summarize, supersedes
from app.services.document_authority_service import DocumentAuthority

TODAY = date(2026, 11, 13)
TENANT = "11111111-1111-1111-1111-111111111111"


def _chunk(doc, text, **extra):
    return {"document_id": doc, "text": text, "score": 0.8, **extra}


def _auth(doc_id, *, approved=False, effective=None, supersedes_id=None, rank=0, tenant=TENANT, active=True, version=1):
    return DocumentAuthority(
        document_id=doc_id, tenant_id=tenant, version=version, effective_date=effective,
        approval_status="approved" if approved else "unverified",
        supersedes_document_id=supersedes_id, authority_rank=rank, is_active=active,
    )


def _numeric_conflict(doc_a="old", doc_b="new"):
    return detect_conflicts([
        _chunk(doc_a, "Refunds are processed within 7 days of purchase."),
        _chunk(doc_b, "Refunds are processed within 14 days of purchase."),
    ])[0]


# ── 1. Numeric contradictions ────────────────────────────────────────────────

def test_numeric_contradiction_detected_with_values():
    c = _numeric_conflict()
    assert c["type"] == "numeric" and c["scope"] == "cross_document"
    assert {c["valueA"], c["valueB"]} == {7.0, 14.0}
    assert "refund" in c["context"]
    assert c["verified"] is True  # exact, no model needed


def test_numeric_requires_specific_shared_subject_not_just_units():
    # Only "days"/"within" in common → different facts, not a conflict.
    assert detect_conflicts([
        _chunk("a", "Refunds are processed within 7 days."),
        _chunk("b", "Shipping takes 2 days within the city."),
    ]) == []


def test_numeric_unit_mismatch_is_not_compared():
    assert detect_conflicts([
        _chunk("a", "Refunds are issued within 7 days of purchase."),
        _chunk("b", "Refunds are issued within 2 weeks of purchase."),
    ]) == []


def test_numeric_identical_values_do_not_conflict():
    assert detect_conflicts([
        _chunk("a", "The plan price is 10 per month."),
        _chunk("b", "Yes — the plan price is 10 per month."),
    ]) == []


# ── 2. Semantic contradictions ───────────────────────────────────────────────

def test_semantic_negation_contradiction_detected():
    conflicts = detect_conflicts([
        _chunk("a", "Refunds are allowed on all subscription plans."),
        _chunk("b", "Refunds are not allowed on any subscription plan."),
    ])
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["type"] == "semantic"
    assert {c["valueA"], c["valueB"]} == {"affirmed", "negated"}
    assert c["verified"] is None  # heuristic until the LLM tier confirms


def test_semantic_antonym_contradiction_detected():
    conflicts = detect_conflicts([
        _chunk("a", "Annual subscriptions are fully refundable."),
        _chunk("b", "Annual subscriptions are non-refundable."),
    ])
    assert [c["type"] for c in conflicts] == ["semantic"]


def test_semantic_same_polarity_is_consistent():
    # "not allowed" and "prohibited" agree → no conflict.
    assert detect_conflicts([
        _chunk("a", "Refunds are not allowed on subscription plans."),
        _chunk("b", "Refunds are prohibited on subscription plans."),
    ]) == []


def test_negation_about_an_unshared_word_does_not_flip_polarity():
    # "no questions asked" negates "questions", not the shared subject.
    assert detect_conflicts([
        _chunk("a", "Refunds are available, no questions asked."),
        _chunk("b", "Refunds are available for every order."),
    ]) == []


@pytest.mark.asyncio
async def test_semantic_candidates_dropped_when_model_says_consistent(monkeypatch):
    from app.services import conflict_detection as cd

    async def fake_verdict(a, b):
        return "CONSISTENT"

    monkeypatch.setattr(cd, "_llm_verdict", fake_verdict)
    monkeypatch.setattr(cd, "_cached_verdict", lambda key: _coro(None))
    monkeypatch.setattr(cd, "_store_verdict", lambda key, v: _coro(None))
    monkeypatch.setattr(settings, "RAG_CONFLICT_LLM_VERIFY", True)
    conflicts = detect_conflicts([
        _chunk("a", "Refunds are allowed on all subscription plans."),
        _chunk("b", "Refunds are not allowed on any subscription plan."),
    ])
    assert len(conflicts) == 1
    assert await cd.verify_semantic_conflicts(conflicts) == []


@pytest.mark.asyncio
async def test_semantic_candidates_confirmed_when_model_says_contradict(monkeypatch):
    from app.services import conflict_detection as cd

    async def fake_verdict(a, b):
        return "CONTRADICT"

    monkeypatch.setattr(cd, "_llm_verdict", fake_verdict)
    monkeypatch.setattr(cd, "_cached_verdict", lambda key: _coro(None))
    monkeypatch.setattr(cd, "_store_verdict", lambda key, v: _coro(None))
    monkeypatch.setattr(settings, "RAG_CONFLICT_LLM_VERIFY", True)
    conflicts = detect_conflicts([
        _chunk("a", "Refunds are allowed on all subscription plans."),
        _chunk("b", "Refunds are not allowed on any subscription plan."),
    ])
    out = await cd.verify_semantic_conflicts(conflicts)
    assert len(out) == 1 and out[0]["verified"] is True


async def _coro(value):
    return value


# ── 3. Conflicting conditions and dates ──────────────────────────────────────

def test_different_conditions_are_not_a_conflict():
    assert detect_conflicts([
        _chunk("a", "Refunds are issued within 7 days for digital products."),
        _chunk("b", "Refunds are issued within 30 days for physical products."),
    ]) == []
    assert detect_conflicts([
        _chunk("a", "If the order is over 100 dollars, shipping is free within 3 days."),
        _chunk("b", "If the order is under 100 dollars, shipping is free within 5 days."),
    ]) == []


def test_same_condition_different_value_is_a_conflict():
    conflicts = detect_conflicts([
        _chunk("a", "Refunds are issued within 7 days for digital products."),
        _chunk("b", "Refunds are issued within 30 days for digital products."),
    ])
    assert len(conflicts) == 1 and conflicts[0]["type"] == "numeric"


def test_conflicting_dates_detected_across_formats():
    conflicts = detect_conflicts([
        _chunk("a", "The new pricing policy takes effect on 1 March 2026."),
        _chunk("b", "The new pricing policy takes effect on 2026-06-01."),
    ])
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["type"] == "date"
    assert {c["valueA"], c["valueB"]} == {"2026-03-01", "2026-06-01"}


def test_year_inside_a_date_is_not_treated_as_a_quantity():
    conflicts = detect_conflicts([
        _chunk("a", "The pricing policy takes effect on 1 March 2026."),
        _chunk("b", "The pricing policy takes effect on 1 March 2027."),
    ])
    assert [c["type"] for c in conflicts] == ["date"]


# ── 4. Approved newer document supersedes an older one ───────────────────────

def test_approved_superseding_document_wins():
    c = _numeric_conflict("old", "new")
    auth = {"old": _auth("old", approved=True), "new": _auth("new", approved=True, supersedes_id="old", version=2)}
    r = resolve_conflict(c, auth, TODAY)
    assert r["status"] == "resolved" and r["winner"] == "new" and r["loser"] == "old"
    assert r["rule"] == "approved_supersession"


def test_supersession_is_transitive_within_tenant():
    auth = {
        "old": _auth("old", approved=True),
        "mid": _auth("mid", approved=True, supersedes_id="old"),
        "new": _auth("new", approved=True, supersedes_id="mid"),
    }
    assert supersedes(auth["new"], auth["old"], auth)
    assert not supersedes(auth["old"], auth["new"], auth)


def test_approved_superseder_not_yet_effective_keeps_current_version():
    c = _numeric_conflict("old", "new")
    auth = {
        "old": _auth("old", approved=True),
        "new": _auth("new", approved=True, supersedes_id="old", effective=date(2027, 1, 1)),
    }
    r = resolve_conflict(c, auth, TODAY)
    assert r["status"] == "resolved" and r["winner"] == "old"
    assert r["rule"] == "superseding_document_not_yet_effective"


# ── 5. Newer but unapproved never overrides approved ─────────────────────────

def test_unapproved_superseding_document_does_not_win():
    c = _numeric_conflict("old", "new")
    auth = {"old": _auth("old", approved=True), "new": _auth("new", supersedes_id="old", version=3)}
    r = resolve_conflict(c, auth, TODAY)
    assert r["status"] == "unresolved" and r["rule"] == "superseding_document_not_approved"


def test_only_approved_source_wins_regardless_of_order():
    c = _numeric_conflict("old", "new")
    for approved in ("old", "new"):
        auth = {"old": _auth("old", approved=approved == "old"), "new": _auth("new", approved=approved == "new")}
        r = resolve_conflict(c, auth, TODAY)
        assert r["status"] == "resolved" and r["winner"] == approved and r["rule"] == "only_approved_source"


def test_upload_recency_version_and_rank_never_decide_between_unapproved_docs():
    c = _numeric_conflict("old", "new")
    auth = {"old": _auth("old", version=1), "new": _auth("new", version=9, rank=50, effective=date(2026, 1, 1))}
    r = resolve_conflict(c, auth, TODAY)
    assert r["status"] == "unresolved" and r["rule"] == "no_approved_source"


def test_both_approved_later_effective_date_and_rank_rules():
    c = _numeric_conflict("old", "new")
    later = {"old": _auth("old", approved=True, effective=date(2025, 1, 1)),
             "new": _auth("new", approved=True, effective=date(2026, 1, 1))}
    assert resolve_conflict(c, later, TODAY)["rule"] == "later_effective_date"
    rank = {"old": _auth("old", approved=True, rank=3), "new": _auth("new", approved=True)}
    r = resolve_conflict(c, rank, TODAY)
    assert r["winner"] == "old" and r["rule"] == "higher_authority_rank"
    clash = {"old": _auth("old", approved=True, effective=date(2025, 1, 1), rank=5),
             "new": _auth("new", approved=True, effective=date(2026, 1, 1))}
    assert resolve_conflict(c, clash, TODAY)["rule"] == "conflicting_authority_evidence"
    equal = {"old": _auth("old", approved=True), "new": _auth("new", approved=True)}
    assert resolve_conflict(c, equal, TODAY)["rule"] == "equal_authority"


# ── 6. Missing authority metadata → unresolved ───────────────────────────────

def test_missing_authority_metadata_is_unresolved():
    c = _numeric_conflict("old", "new")
    assert resolve_conflict(c, {}, TODAY)["rule"] == "missing_authority_metadata"
    assert resolve_conflict(c, {"old": _auth("old", approved=True)}, TODAY)["rule"] == "missing_authority_metadata"


def test_default_rows_never_resolve():
    c = _numeric_conflict("old", "new")
    r = resolve_conflict(c, {"old": _auth("old"), "new": _auth("new")}, TODAY)
    assert r["status"] == "unresolved" and r["winner"] is None


def test_intra_document_conflicts_are_never_resolved():
    c = detect_conflicts([
        _chunk("a", "The plan price is 10 dollars per month."),
        _chunk("a", "The plan price is 15 dollars per month."),
    ])[0]
    assert resolve_conflict(c, {"a": _auth("a", approved=True)}, TODAY)["rule"] == "same_document"


async def _fresh_engine():
    """pytest-asyncio gives each test its own event loop; asyncpg
    connections pooled by a previous test's loop can't be reused on this
    one, so start every DB-backed test with an empty pool."""
    from app.database import async_engine
    await async_engine.dispose()


# ── 7. Cross-tenant isolation ────────────────────────────────────────────────

def test_resolver_ignores_supersession_links_to_documents_outside_tenant_map():
    # A tenant-scoped authority map never contains another tenant's document,
    # so a (hypothetical) foreign link can never be followed to a win.
    c = _numeric_conflict("old", "new")
    auth = {"new": _auth("new", approved=True, supersedes_id="foreign-doc"), "old": _auth("old", approved=True)}
    r = resolve_conflict(c, auth, TODAY)
    assert r["rule"] in ("equal_authority",)  # no supersession applied
    assert not supersedes(auth["new"], auth["old"], auth)


@pytest.mark.asyncio
async def test_database_rejects_cross_tenant_and_self_supersession():
    """The composite FK and CHECK constraint hold at the database level,
    independent of application code. Uses two existing tenants in the
    configured database; skips when unavailable."""
    from sqlalchemy import select, text
    from sqlalchemy.exc import IntegrityError

    from app.database import AsyncSessionLocal
    from app.models.document import Document, DocumentSource, DocumentStatus

    await _fresh_engine()
    try:
        async with AsyncSessionLocal() as db:
            tenants = (await db.execute(text("select id from tenants where is_active = true limit 2"))).scalars().all()
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"database unavailable: {e}")
    if len(tenants) < 2:
        pytest.skip("need two tenants")
    t1, t2 = tenants

    def _doc(tenant_id, name):
        return Document(tenant_id=tenant_id, original_filename=name, file_size_mb=0.0, mime_type="text/plain",
                        source=DocumentSource.faq, status=DocumentStatus.ready, question=name, answer="x")

    async with AsyncSessionLocal() as db:
        a, b = _doc(t1, "qa-isolation-a"), _doc(t2, "qa-isolation-b")
        db.add_all([a, b])
        await db.commit()
        a_id, b_id = a.id, b.id
    try:
        # cross-tenant link → FK violation
        async with AsyncSessionLocal() as db:
            doc = (await db.execute(select(Document).where(Document.id == a_id))).scalar_one()
            doc.supersedes_document_id = b_id
            with pytest.raises(IntegrityError):
                await db.commit()
            await db.rollback()
        # self link → CHECK violation
        async with AsyncSessionLocal() as db:
            doc = (await db.execute(select(Document).where(Document.id == a_id))).scalar_one()
            doc.supersedes_document_id = a_id
            with pytest.raises(IntegrityError):
                await db.commit()
            await db.rollback()
        # service layer: foreign target is simply "not found"
        from fastapi import HTTPException
        from app.services.document_authority_service import update_authority
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc:
                await update_authority(
                    document_id=a_id, tenant_id=t1, actor_user_id=uuid.uuid4(),
                    fields_set={"supersedesDocumentId"}, supersedes_document_id=str(b_id), db=db,
                )
            assert exc.value.status_code == 404
    finally:
        async with AsyncSessionLocal() as db:
            for did in (a_id, b_id):
                d = (await db.execute(select(Document).where(Document.id == did))).scalar_one_or_none()
                if d is not None:
                    await db.delete(d)
            await db.commit()


@pytest.mark.asyncio
async def test_authority_map_is_tenant_scoped():
    """load_authority_map never returns another tenant's document even when
    its id is asked for explicitly."""
    from sqlalchemy import select, text

    from app.database import AsyncSessionLocal
    from app.models.document import Document, DocumentSource, DocumentStatus
    from app.services.document_authority_service import load_authority_map

    await _fresh_engine()
    try:
        async with AsyncSessionLocal() as db:
            tenants = (await db.execute(text("select id from tenants where is_active = true limit 2"))).scalars().all()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"database unavailable: {e}")
    if len(tenants) < 2:
        pytest.skip("need two tenants")
    t1, t2 = tenants
    async with AsyncSessionLocal() as db:
        foreign = Document(tenant_id=t2, original_filename="qa-foreign", file_size_mb=0.0, mime_type="text/plain",
                           source=DocumentSource.faq, status=DocumentStatus.ready, question="q", answer="a",
                           approval_status="approved")
        db.add(foreign)
        await db.commit()
        foreign_id = foreign.id
    try:
        async with AsyncSessionLocal() as db:
            assert await load_authority_map(t1, [str(foreign_id)], db) == {}
            assert str(foreign_id) in await load_authority_map(t2, [str(foreign_id)], db)
    finally:
        async with AsyncSessionLocal() as db:
            d = (await db.execute(select(Document).where(Document.id == foreign_id))).scalar_one_or_none()
            if d is not None:
                await db.delete(d)
            await db.commit()


# ── 8. Unresolved conflicts → correct fallback ───────────────────────────────

def _metrics_with(conflict, resolution):
    return {"conflicts": [{**conflict, "resolution": resolution}], "conflictSummary": summarize([resolution])}


def test_disclosure_is_appended_when_model_omits_one_side():
    c = _numeric_conflict("old", "new")
    unresolved = [{**c, "resolution": {"status": "unresolved"}}]
    names = {"old": "Policy-2024.pdf", "new": "Policy-2026.pdf"}
    out = rag_service._ensure_conflict_disclosure("Refunds take 7 days.", unresolved, names, True)
    assert "Policy-2024.pdf says 7" in out and "Policy-2026.pdf says 14" in out
    assert out.startswith("Refunds take 7 days.")
    # Already disclosed → untouched.
    already = "Our documents disagree: Policy-2024.pdf says 7 days while Policy-2026.pdf says 14 days."
    assert rag_service._ensure_conflict_disclosure(already, unresolved, names, True) == already
    # Sources hidden → no document names leak.
    hidden = rag_service._ensure_conflict_disclosure("Refunds take 7 days.", unresolved, names, False)
    assert "Policy-2024.pdf" not in hidden and "one of our sources says 7" in hidden


def test_unresolved_conflict_blocks_generation_in_handoff_and_refuse_modes(monkeypatch):
    c = _numeric_conflict("old", "new")
    metrics = _metrics_with(c, {"status": "unresolved", "rule": "no_approved_source"})
    monkeypatch.setattr(settings, "RAG_UNRESOLVED_CONFLICT_ACTION", "handoff")
    r = rag_service._conflict_block_result(metrics, "fallback!", True, {}, True)
    assert r["handoff"] is True and r["refused"] is False and r["conflict"] is True and r["sources"] == []
    r = rag_service._conflict_block_result(metrics, "fallback!", False, {}, True)
    assert r["handoff"] is False and r["refused"] is True  # handoff disabled → refuse
    monkeypatch.setattr(settings, "RAG_UNRESOLVED_CONFLICT_ACTION", "refuse")
    r = rag_service._conflict_block_result(metrics, "fallback!", True, {}, True)
    assert r["refused"] is True and r["answer"] == "fallback!"
    monkeypatch.setattr(settings, "RAG_UNRESOLVED_CONFLICT_ACTION", "disclose")
    assert rag_service._conflict_block_result(metrics, "fallback!", True, {}, True) is None


def test_resolved_conflict_never_blocks_and_drops_loser_chunk():
    chunks = [
        _chunk("old", "Refunds are processed within 7 days of purchase."),
        _chunk("new", "Refunds are processed within 14 days of purchase."),
        _chunk("old", "Our office is open Monday to Friday."),
    ]
    c = detect_conflicts(chunks)[0]
    res = {"status": "resolved", "rule": "only_approved_source", "winner": "new", "loser": "old"}
    kept = rag_service._drop_superseded_chunks(chunks, [c], [res])
    assert [k["document_id"] for k in kept] == ["new", "old"]  # only the contradicted chunk went
    res_sup = {**res, "rule": "approved_supersession"}
    kept = rag_service._drop_superseded_chunks(chunks, [c], [res_sup])
    assert [k["document_id"] for k in kept] == ["new"]  # whole superseded document went
    metrics = _metrics_with(c, res)
    assert rag_service._unresolved_conflicts(metrics) == []
    assert rag_service._conflict_block_result(metrics, "fb", True, {}, True) is None


def test_prompt_note_distinguishes_resolved_and_unresolved():
    c = _numeric_conflict("old", "new")
    names = {"old": "Old.pdf", "new": "New.pdf"}
    unresolved_note = conflict_prompt_note([c], names)
    assert "CONTRADICT" in unresolved_note and "Do not silently pick one" in unresolved_note
    resolved_note = conflict_prompt_note([c], names, [{"status": "resolved", "winner": "new", "loser": "old", "reason": "approved"}])
    assert "New.pdf is the authoritative" in resolved_note and "cite New.pdf" in resolved_note
    assert conflict_prompt_note([], names) == ""


# ── 9. No conflict → existing behaviour unchanged ────────────────────────────

def test_no_conflict_leaves_pipeline_inputs_untouched():
    chunks = [
        _chunk("a", "Our office opened in 2019 near the station."),
        _chunk("b", "Delivery takes 5 business days on average."),
    ]
    assert detect_conflicts(chunks) == []
    assert rag_service._drop_superseded_chunks(chunks, [], []) is chunks
    assert rag_service._unresolved_conflicts({"conflicts": []}) == []
    assert rag_service._conflict_block_result({"conflicts": []}, "fb", True, {}, True) is None
    assert rag_service._ensure_conflict_disclosure("answer", [], {}, True) == "answer"
    assert conflict_prompt_note([], {}) == ""
    assert summarize([]) == {"total": 0, "resolved": 0, "unresolved": 0}


# ── Authority API schema ─────────────────────────────────────────────────────

def test_authority_request_validates_date_and_ranges():
    ok = fe.FEUpdateDocumentAuthorityRequest(effectiveDate="2026-03-01", version=2, approvalStatus="approved", authorityRank=10)
    assert ok.effectiveDate == "2026-03-01" and ok.model_fields_set == {"effectiveDate", "version", "approvalStatus", "authorityRank"}
    assert fe.FEUpdateDocumentAuthorityRequest(effectiveDate="").effectiveDate is None
    with pytest.raises(ValidationError):
        fe.FEUpdateDocumentAuthorityRequest(effectiveDate="01/03/2026")
    with pytest.raises(ValidationError):
        fe.FEUpdateDocumentAuthorityRequest(version=0)
    with pytest.raises(ValidationError):
        fe.FEUpdateDocumentAuthorityRequest(authorityRank=101)
    with pytest.raises(ValidationError):
        fe.FEUpdateDocumentAuthorityRequest(approvalStatus="verified")


# ── Candidate-pool scanning (rag_service._promote_conflicting_chunks) ────────

def test_bare_years_are_not_quantities():
    assert detect_conflicts([
        _chunk("a", "Refund Policy 2024. Refunds are processed within 30 days of purchase."),
        _chunk("b", "Refund policy: full refunds within 30 days of purchase."),
    ]) == []
    conflicts = detect_conflicts([
        _chunk("a", "Refund Policy 2024. Refunds are processed within 30 days of purchase."),
        _chunk("b", "Refund Policy 2026. Refunds are processed within 7 days of purchase."),
    ])
    assert len(conflicts) == 1 and {conflicts[0]["valueA"], conflicts[0]["valueB"]} == {30.0, 7.0}


def test_conflicting_pool_chunk_is_promoted_into_context_with_remapped_indices():
    pool = [
        _chunk("p", "Our office is open Monday to Friday."),            # 0 top-k
        _chunk("old", "Refunds are processed within 30 days of purchase."),  # 1 top-k
        _chunk("x", "We ship worldwide."),                               # 2 top-k
        _chunk("y", "Support hours are 9 to 5."),                       # 3 tail
        _chunk("new", "Refunds are processed within 7 days of purchase."),   # 4 tail
    ]
    conflicts = detect_conflicts(pool)
    assert len(conflicts) == 1 and {conflicts[0]["chunkA"], conflicts[0]["chunkB"]} == {1, 4}
    chunks, remapped = rag_service._promote_conflicting_chunks(pool, 3, conflicts)
    assert [c["document_id"] for c in chunks] == ["p", "old", "x", "new"]  # tail chunk 4 promoted, 3 not
    assert {remapped[0]["chunkA"], remapped[0]["chunkB"]} == {1, 3}
    res = {"status": "resolved", "rule": "approved_supersession", "winner": "new", "loser": "old"}
    final = rag_service._drop_superseded_chunks(chunks, remapped, [res])
    assert [c["document_id"] for c in final] == ["p", "x", "new"]


def test_pool_only_conflicts_are_ignored():
    # Two tail chunks contradicting each other, neither in the context → not
    # relevant to the answer; rag_service filters these before resolution.
    pool = [
        _chunk("p", "Our office is open Monday to Friday."),
        _chunk("old", "Refunds are processed within 30 days of purchase."),
        _chunk("new", "Refunds are processed within 7 days of purchase."),
    ]
    conflicts = detect_conflicts(pool)
    top_k = 1
    relevant = [c for c in conflicts if c["chunkA"] < top_k or c["chunkB"] < top_k]
    assert conflicts and relevant == []


# ── Review follow-ups (2026-11-13) ───────────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    ("The Basic plan costs 10 dollars per month.", "The Pro plan costs 20 dollars per month."),
    ("Standard shipping takes 5 business days.", "Express shipping takes 2 business days."),
    ("Refunds are processed within 7 days of purchase.", "Exchanges are processed within 14 days of purchase."),
    ("Our summer sale starts on 1 June 2026.", "Our winter sale starts on 1 December 2026."),
    ("The Basic plan includes phone support.", "The Pro plan does not include phone support."),
])
def test_sibling_facts_are_not_contradictions(a, b):
    assert detect_conflicts([_chunk("a", a), _chunk("b", b)]) == []


def test_intra_document_price_list_is_not_a_conflict():
    assert detect_conflicts([_chunk("a", "Pricing\n- Basic plan: 10 dollars per month\n- Pro plan: 20 dollars per month")]) == []


def test_one_sided_qualifier_and_rewording_still_detected():
    # "full refunds" vs "refunds": a general rule and a specific one can clash.
    assert len(detect_conflicts([
        _chunk("p", "Refund policy: full refunds within 30 days of purchase, no questions asked."),
        _chunk("f", "Refunds are processed within 7 days of purchase."),
    ])) == 1
    assert len(detect_conflicts([
        _chunk("a", "Customers may request a refund within 30 days of purchase."),
        _chunk("b", "A refund can be requested within 14 days of purchase."),
    ])) == 1


def test_intra_document_conflicts_are_left_out_of_the_prompt_note():
    c = detect_conflicts([
        _chunk("a", "The plan price is 10 dollars per month."),
        _chunk("a", "The plan price is 15 dollars per month."),
    ])
    assert c and c[0]["scope"] == "intra_document"
    assert conflict_prompt_note(c, {"a": "Pricing.pdf"}) == ""


def test_context_size_filters_tail_only_conflicts_before_the_cap():
    pool = [
        _chunk("p", "Our office is open Monday to Friday."),
        _chunk("old", "Refunds are processed within 30 days of purchase."),
        _chunk("new", "Refunds are processed within 7 days of purchase."),
    ]
    assert detect_conflicts(pool) != []
    assert detect_conflicts(pool, context_size=1) == []
    assert len(detect_conflicts(pool, context_size=2)) == 1


def test_value_mentioned_uses_digit_boundaries():
    assert rag_service._value_mentioned("the warranty lasts 1 year (see section 12).", 1.0)
    # "12" must not satisfy 1 or 2; a bare "2" elsewhere in the text would,
    # which is the accepted limit of a value-presence check.
    assert not rag_service._value_mentioned("the warranty lasts 1 year (see section 12).", 2.0)
    assert not rag_service._value_mentioned("the warranty lasts 12 months.", 1.0)
    assert not rag_service._value_mentioned("founded in 2017.", 7.0)
    assert rag_service._value_mentioned("it takes 7 days.", 7.0)
    assert rag_service._value_mentioned("effective 1 march 2026.", "2026-03-01")


def test_heuristic_semantic_candidates_never_block(monkeypatch):
    c = detect_conflicts([
        _chunk("a", "Refunds are allowed on all subscription plans."),
        _chunk("b", "Refunds are not allowed on any subscription plan."),
    ])[0]
    assert c["verified"] is None
    metrics = _metrics_with(c, {"status": "unresolved", "rule": "no_approved_source"})
    monkeypatch.setattr(settings, "RAG_UNRESOLVED_CONFLICT_ACTION", "refuse")
    assert rag_service._conflict_block_result(metrics, "fb", True, {}, True) is None
    confirmed = _metrics_with({**c, "verified": True}, {"status": "unresolved", "rule": "no_approved_source"})
    blocked = rag_service._conflict_block_result(confirmed, "fb", True, {}, True)
    assert blocked is not None and blocked["conflict_blocked"] is True


def test_deprecated_superseder_falls_through_to_approval_rules():
    c = _numeric_conflict("old", "new")
    auth = {
        "old": _auth("old", approved=True),
        "new": DocumentAuthority(document_id="new", tenant_id=TENANT, version=2, effective_date=None,
                                 approval_status="deprecated", supersedes_document_id="old", authority_rank=0, is_active=True),
    }
    r = resolve_conflict(c, auth, TODAY)
    assert r["status"] == "resolved" and r["winner"] == "old" and r["rule"] == "only_approved_source"
