"""
One-time migration: move every tenant's Pinecone vectors from the default
namespace into a per-tenant namespace (Phase 2 / Indexing, Cluster F — see
PHASE2_INDEXING_FIX_PLAN.md).

Why this exists: vector_store.py used to write all tenants' vectors into
Pinecone's default namespace, isolated only by a `tenant_id` metadata filter.
That filter is applied correctly everywhere today, but it's a convention, not
a structural guarantee — a future query path that forgets it would silently
leak cross-tenant data. Namespaces make that isolation structural. Existing
vectors (written before this migration) still live in the default namespace
and won't be found by namespace-scoped queries until they're moved.

How: rather than fetching and re-inserting raw vectors (Postgres doesn't
persist per-chunk vector IDs, only chunk_count), this purges each `ready`
document's default-namespace vectors and re-runs the normal indexing
pipeline, which now writes directly into the tenant's namespace (see
vector_store.upsert_chunks). This re-embeds every document — a real,
one-time Pinecone Inference API cost — which is why this is a manually-run
script, not an automatic startup step.

Run once, manually, after deploying the Cluster F code change:
    cd Backend && python -m scripts.migrate_vectors_to_namespaces

Safe to re-run: documents already reprocessed under this run end up
`pending` -> `ready` again with fresh chunk_counts; running it twice just
re-embeds a second time (wasteful, not incorrect).
"""

import asyncio
import logging

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.document import Document, DocumentStatus
from app.core.vector_store import delete_document_chunks
from app.services.indexing_service import schedule_document_processing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger("migrate_vectors_to_namespaces")

async def main() -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Document).where(
                Document.is_active == True,
                Document.status == DocumentStatus.ready,
            )
        )
        docs = result.scalars().all()

    if not docs:
        logger.info("No ready documents found — nothing to migrate.")
        return

    logger.info("Migrating %d document(s) into per-tenant Pinecone namespaces...", len(docs))

    doc_ids = []
    for doc in docs:
        try:
            await delete_document_chunks(str(doc.tenant_id), str(doc.id))
        except Exception:
            logger.exception("Failed to purge old vectors for document %s — skipping it", doc.id)
            continue
        doc_ids.append(doc.id)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Document).where(Document.id.in_(doc_ids)))
        for doc in result.scalars().all():
            doc.status = DocumentStatus.pending
        await db.commit()

    schedule_document_processing(doc_ids)
    logger.info(
        "Rescheduled %d document(s) for reindexing into their namespace. "
        "This runs in the background — check each document's status on the "
        "Knowledge page (or the documents table) to confirm they all reach "
        "'ready' again.",
        len(doc_ids),
    )
    # Give the fire-and-forget indexing tasks a chance to actually run before
    # the script's event loop exits (schedule_document_processing only
    # kicks them off, it doesn't await completion).
    await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
