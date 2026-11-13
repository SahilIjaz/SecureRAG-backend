"""
Deterministic, evidence-based resolution of detected contradictions.

Given one conflict (see conflict_detection._make_conflict) and the authority
snapshots of the documents involved (document_authority_service), decide —
without any model call, similarity score or upload timestamp — whether one
document is the *verified, currently applicable* source for that point.

Rules, in order; the first that applies decides:

  0. Same-document conflicts are never resolved (nothing to prefer).
  1. Either document has no authority snapshot (not found for this tenant)
     → unresolved: "missing_authority_metadata".
  2. Explicit supersession (A supersedes B, directly or through a chain):
       superseder approved and effective today   → superseder wins
       superseder approved but not yet effective → the superseded doc wins
                                                   only if it is itself
                                                   approved and effective,
                                                   else unresolved
       superseder not approved                   → unresolved
  3. Exactly one document is approved AND effective today → it wins.
  4. Both approved and effective:
       ranks differ and effective dates differ in the same direction → wins
       ranks differ and dates point the other way → unresolved
                                                   ("conflicting_authority_evidence")
       only ranks differ → higher authority_rank wins
       only dates differ → later effective date wins
       otherwise → unresolved ("equal_authority")
  5. Neither approved → unresolved ("no_approved_source") — an unverified
     document never wins, whatever its rank, version or upload date.

What is deliberately NOT evidence: created_at / "newest upload", vector or
rerank similarity, the LLM's self-reported confidence, document version
numbers across different lineages. Version is informational.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, Optional

from app.services.document_authority_service import DocumentAuthority, MAX_SUPERSESSION_DEPTH


def _result(status: str, rule: str, reason: str, winner: Optional[str] = None, loser: Optional[str] = None) -> dict:
    return {"status": status, "rule": rule, "reason": reason, "winner": winner, "loser": loser}


def _unresolved(rule: str, reason: str) -> dict:
    return _result("unresolved", rule, reason)


def _resolved(rule: str, reason: str, winner: DocumentAuthority, loser: DocumentAuthority) -> dict:
    return _result("resolved", rule, reason, winner.document_id, loser.document_id)


def supersedes(newer: DocumentAuthority, older: DocumentAuthority, authority: Dict[str, DocumentAuthority]) -> bool:
    """True when `newer` supersedes `older` directly or through a chain of
    documents all visible in `authority` (which is tenant-scoped, so a chain
    can never cross a tenant boundary). Bounded walk; cycles can't be
    created through the service, but a corrupted link still terminates."""
    seen = set()
    current = newer
    for _ in range(MAX_SUPERSESSION_DEPTH):
        nxt = current.supersedes_document_id
        if not nxt or nxt in seen:
            return False
        if nxt == older.document_id:
            return True
        seen.add(nxt)
        current = authority.get(nxt)
        if current is None:
            return False
    return False


def _label(a: DocumentAuthority) -> str:
    bits = [f"v{a.version}", a.approval_status]
    if a.effective_date:
        bits.append(f"effective {a.effective_date.isoformat()}")
    return ", ".join(bits)


def resolve_conflict(conflict: dict, authority: Dict[str, DocumentAuthority], today: Optional[date] = None) -> dict:
    """Decide one conflict. Pure function of (conflict, authority, today)."""
    today = today or date.today()

    if conflict.get("scope") == "intra_document" or conflict.get("documentA") == conflict.get("documentB"):
        return _unresolved("same_document", "both statements come from the same document")

    a = authority.get(str(conflict.get("documentA")))
    b = authority.get(str(conflict.get("documentB")))
    if a is None or b is None:
        return _unresolved("missing_authority_metadata", "no verified authority metadata for one of the sources")

    # 2. explicit supersession, either direction
    for newer, older in ((a, b), (b, a)):
        if supersedes(newer, older, authority):
            if newer.approved and newer.applicable_on(today):
                return _resolved("approved_supersession",
                                 f"approved and explicitly supersedes the other ({_label(newer)})", newer, older)
            if newer.approved and not newer.applicable_on(today):
                if older.approved and older.applicable_on(today):
                    return _resolved("superseding_document_not_yet_effective",
                                     f"the replacement takes effect {newer.effective_date.isoformat()}; the approved current version still applies",
                                     older, newer)
                return _unresolved("superseding_document_not_yet_effective",
                                   f"the superseding document only takes effect {newer.effective_date.isoformat()} and the older one is not approved")
            return _unresolved("superseding_document_not_approved",
                               "a newer document claims to supersede the other but has not been approved")

    a_ok = a.approved and a.applicable_on(today)
    b_ok = b.approved and b.applicable_on(today)

    # 3. exactly one approved + currently effective
    if a_ok != b_ok:
        winner, loser = (a, b) if a_ok else (b, a)
        return _resolved("only_approved_source", f"the only approved, currently effective source ({_label(winner)})", winner, loser)

    # 5. neither approved → never auto-resolve
    if not a_ok and not b_ok:
        return _unresolved("no_approved_source", "neither source has been approved by a workspace admin")

    # 4. both approved + effective
    rank_pref = None
    if a.authority_rank != b.authority_rank:
        rank_pref = a if a.authority_rank > b.authority_rank else b
    date_pref = None
    if a.effective_date and b.effective_date and a.effective_date != b.effective_date:
        date_pref = a if a.effective_date > b.effective_date else b

    if rank_pref and date_pref:
        if rank_pref is date_pref:
            loser = b if rank_pref is a else a
            return _resolved("later_effective_date_and_higher_rank",
                             f"later effective date and higher priority ({_label(rank_pref)})", rank_pref, loser)
        return _unresolved("conflicting_authority_evidence",
                           "the higher-priority source is older than the other approved source")
    if rank_pref:
        loser = b if rank_pref is a else a
        return _resolved("higher_authority_rank", f"higher admin-assigned priority ({_label(rank_pref)})", rank_pref, loser)
    if date_pref:
        loser = b if date_pref is a else a
        return _resolved("later_effective_date", f"later effective date ({_label(date_pref)})", date_pref, loser)
    return _unresolved("equal_authority", "both sources are approved with equal priority and no distinguishing effective date")


def summarize(resolutions: list[dict]) -> dict:
    resolved = sum(1 for r in resolutions if r.get("status") == "resolved")
    return {"total": len(resolutions), "resolved": resolved, "unresolved": len(resolutions) - resolved}
