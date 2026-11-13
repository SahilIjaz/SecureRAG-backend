"""
Document authority metadata — the trusted, admin-maintained facts the
conflict resolver is allowed to reason from.

Three jobs:

  * `update_authority()`   — validate and apply an admin's change to one
                             document's version / effective date / approval
                             status / supersession link / rank.
  * `load_authority_map()` — tenant-scoped read of those fields for the
                             documents involved in a detected conflict
                             (used by rag_service at answer time).
  * `superseded_by_map()`  — reverse index (old → new) for the Knowledge UI.

Security properties, enforced here and at the database:

  * Only the owner of the row's tenant can read or write it: every query
    filters on tenant_id, and the composite FK
    fk_documents_supersedes_same_tenant (see models/document.py) rejects a
    cross-tenant supersedes link even if application code ever regressed.
  * Nothing in ingestion, retrieval or generation calls `update_authority`.
    Document text and LLM output can therefore never grant authority.
  * A document can't supersede itself, an inactive document, a document of
    another tenant, or form a cycle (A→B→A), so the resolver's chain walk
    always terminates.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import ApprovalStatus, Document

logger = logging.getLogger(__name__)

# Hard stop for the supersession chain walk — a tenant will never have a
# legitimate chain this long; the cap only guards against a pathological
# loop that somehow bypassed the cycle check.
MAX_SUPERSESSION_DEPTH = 50


@dataclass(frozen=True)
class DocumentAuthority:
    """Plain, immutable snapshot of the authority fields of one document —
    what the resolver sees. `is_active` is included so a deprecated-by-
    deletion document never wins anything."""
    document_id: str
    tenant_id: str
    version: int
    effective_date: Optional[date]
    approval_status: str
    supersedes_document_id: Optional[str]
    authority_rank: int
    is_active: bool

    @property
    def approved(self) -> bool:
        return self.approval_status == ApprovalStatus.approved.value and self.is_active

    def applicable_on(self, today: date) -> bool:
        """Is this document in force on `today`? No effective date means
        "always" (the admin didn't scope it in time)."""
        return self.effective_date is None or self.effective_date <= today

    @classmethod
    def from_document(cls, doc: Document) -> "DocumentAuthority":
        return cls(
            document_id=str(doc.id),
            tenant_id=str(doc.tenant_id),
            version=int(doc.version or 1),
            effective_date=doc.effective_date,
            approval_status=doc.approval_status or ApprovalStatus.unverified.value,
            supersedes_document_id=str(doc.supersedes_document_id) if doc.supersedes_document_id else None,
            authority_rank=int(doc.authority_rank or 0),
            is_active=bool(doc.is_active),
        )


# ── Reads ─────────────────────────────────────────────────────────────────────

async def load_authority_map(
    tenant_id: uuid.UUID | str, document_ids: Iterable[str], db: AsyncSession
) -> dict[str, DocumentAuthority]:
    """Authority snapshots for `document_ids`, **restricted to `tenant_id`**.

    A document id that belongs to another tenant is simply absent from the
    result — the resolver then treats it as "no authority evidence" and
    leaves the conflict unresolved. Also follows supersession links one hop
    so the resolver can see the document a retrieved one claims to replace
    even when that older document wasn't itself retrieved.
    """
    ids: set[uuid.UUID] = set()
    for d in document_ids:
        try:
            ids.add(uuid.UUID(str(d)))
        except (ValueError, TypeError):
            continue
    if not ids:
        return {}
    tid = uuid.UUID(str(tenant_id))

    out: dict[str, DocumentAuthority] = {}
    pending = set(ids)
    for _ in range(2):  # the requested docs, then whatever they supersede
        if not pending:
            break
        rows = (await db.execute(
            select(Document).where(Document.tenant_id == tid, Document.id.in_(pending))
        )).scalars().all()
        pending = set()
        for doc in rows:
            auth = DocumentAuthority.from_document(doc)
            out[auth.document_id] = auth
            if doc.supersedes_document_id and str(doc.supersedes_document_id) not in out:
                pending.add(doc.supersedes_document_id)
    return out


def superseded_by_map(docs: Iterable[Document]) -> dict[str, Document]:
    """{old_document_id: newer_document} for one tenant's active documents,
    so the UI can show "superseded by X" on the old row."""
    out: dict[str, Document] = {}
    for d in docs:
        if d.supersedes_document_id and d.is_active:
            out[str(d.supersedes_document_id)] = d
    return out


# ── Writes ────────────────────────────────────────────────────────────────────

async def _get_own_active_document(doc_id: uuid.UUID, tenant_id: uuid.UUID, db: AsyncSession) -> Document:
    doc = (await db.execute(
        select(Document).where(
            Document.id == doc_id, Document.tenant_id == tenant_id, Document.is_active == True,
        )
    )).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    return doc


async def _assert_no_cycle(start: Document, target_id: uuid.UUID, tenant_id: uuid.UUID, db: AsyncSession) -> None:
    """Walking `supersedes` from the proposed target must never reach
    `start`; otherwise A→B→A would make the resolver's chain walk loop."""
    seen = {start.id}
    current = target_id
    for _ in range(MAX_SUPERSESSION_DEPTH):
        if current in seen:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="That would create a supersession loop (the target already derives from this document).",
            )
        seen.add(current)
        nxt = (await db.execute(
            select(Document.supersedes_document_id).where(
                Document.id == current, Document.tenant_id == tenant_id,
            )
        )).scalar_one_or_none()
        if nxt is None:
            return
        current = nxt
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="Supersession chain is too long to validate.",
    )


async def update_authority(
    *,
    document_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    fields_set: set[str],
    version: Optional[int] = None,
    effective_date: Optional[str] = None,
    approval_status: Optional[str] = None,
    supersedes_document_id: Optional[str] = None,
    authority_rank: Optional[int] = None,
    db: AsyncSession,
) -> Document:
    """Apply a partial authority update. `fields_set` says which keyword
    arguments were actually present in the request (so `None` can mean
    "clear" for the nullable fields rather than "leave alone").

    The caller is responsible for the role check (admin+); this function
    is responsible for every data-integrity rule.
    """
    doc = await _get_own_active_document(document_id, tenant_id, db)

    if "version" in fields_set and version is not None:
        doc.version = int(version)

    if "effectiveDate" in fields_set:
        doc.effective_date = date.fromisoformat(effective_date) if effective_date else None

    if "approvalStatus" in fields_set and approval_status is not None:
        if approval_status not in {s.value for s in ApprovalStatus}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid approval status.")
        doc.approval_status = approval_status

    if "authorityRank" in fields_set and authority_rank is not None:
        doc.authority_rank = int(authority_rank)

    if "supersedesDocumentId" in fields_set:
        if not supersedes_document_id:
            doc.supersedes_document_id = None
        else:
            try:
                target_id = uuid.UUID(str(supersedes_document_id))
            except ValueError:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Superseded document not found.")
            if target_id == doc.id:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="A document cannot supersede itself.",
                )
            # Same-tenant + active check at the application layer; the
            # composite FK repeats the tenant check at the database layer.
            await _get_own_active_document(target_id, tenant_id, db)
            await _assert_no_cycle(doc, target_id, tenant_id, db)
            doc.supersedes_document_id = target_id

    doc.authority_updated_at = datetime.now(timezone.utc)
    doc.authority_updated_by = actor_user_id
    await db.commit()
    await db.refresh(doc)
    logger.info(
        "document authority updated tenant=%s doc=%s by=%s fields=%s",
        tenant_id, doc.id, actor_user_id, sorted(fields_set),
    )
    return doc
