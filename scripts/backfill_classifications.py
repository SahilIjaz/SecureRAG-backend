"""
Backfill topic / sentiment / resolution status for conversations that predate
the classification service.

Usage (from the SecureRAG-backend directory):
    ./venv/bin/python -m scripts.backfill_classifications           # unclassified only
    ./venv/bin/python -m scripts.backfill_classifications --all     # re-classify everything
"""

import asyncio
import sys

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.conversation import Conversation
from app.services.classification_service import classify_conversation

async def main(reclassify_all: bool = False) -> None:
    async with AsyncSessionLocal() as db:
        query = select(Conversation.id).order_by(Conversation.created_at)
        if not reclassify_all:
            query = query.where(Conversation.topic.is_(None))
        ids = list((await db.execute(query)).scalars().all())

    if not ids:
        print("Nothing to backfill — every conversation already has a topic.")
        return

    print(f"Classifying {len(ids)} conversation(s)...")
    done = failed = 0
    for cid in ids:
        try:
            result = await classify_conversation(cid)
            if result is None:
                print(f"  {cid}: skipped (no messages or classifier unavailable)")
            else:
                done += 1
                print(f"  {cid}: {result['topic']} / {result['sentiment']} / answered={result['answered']}")
        except Exception as e:
            failed += 1
            print(f"  {cid}: FAILED — {e}")

    print(f"\nDone: {done} classified, {failed} failed, {len(ids) - done - failed} skipped.")

if __name__ == "__main__":
    asyncio.run(main("--all" in sys.argv))
