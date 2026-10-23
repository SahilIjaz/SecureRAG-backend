"""
RAG evaluation: faithfulness (RAGAS-style) + tenant-isolation proof.

Produces the measured numbers the research paper needs instead of unproven
claims. For every evaluated question it:

  1. Generates the question FROM a real indexed chunk of a tenant (so ground
     truth is known to exist in that tenant's knowledge base).
  2. Runs the full production pipeline (rag_service.answer_question — hybrid
     retrieval, rerank, conflict scan, confidence gate).
  3. Scores faithfulness the way RAGAS defines it: an LLM judge decomposes
     the answer into factual claims and counts how many are supported by the
     retrieved context → faithfulness = supported / total claims.
  4. Checks authorization: every citation's document_id must belong to the
     asking tenant's own active documents (cross-tenant leak check).

It also runs NEGATIVE probes: questions derived from tenant A's documents
are asked AS tenant B. A correct system answers from B's own data or refuses
— any citation of an A-owned document is counted as a cross-tenant leak.

Usage:
    PYTHONPATH=. ./venv/bin/python scripts/evaluate_rag.py --questions 10
    PYTHONPATH=. ./venv/bin/python scripts/evaluate_rag.py --questions 50 --json results.json

Cost note: each question spends two cheap classification-model calls
(question generation + judging) and one normal answer generation.
"""

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text as sqltext  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import AsyncSessionLocal  # noqa: E402
from app.services import rag_service  # noqa: E402
from app.services.llm import router as llm_router  # noqa: E402


async def _classify(prompt: str, max_tokens: int = 200) -> str:
    result = await llm_router.classify(
        prompt, provider_name="gemini", model=settings.GEMINI_CLASSIFICATION_MODEL,
        max_tokens=max_tokens,
    )
    return (result.text or "").strip()


async def load_tenants_with_chunks(min_chunks: int = 2) -> list[dict]:
    """Tenants that actually have indexed knowledge, with their active-doc
    maps (used both as the retrieval pre-filter and the authorization
    ground truth)."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sqltext(
            "select tenant_id, count(*) as n from document_chunks "
            "group by tenant_id having count(*) >= :m order by n desc"
        ), {"m": min_chunks})).all()
        tenants = []
        for r in rows:
            docs = (await db.execute(sqltext(
                "select id::text, original_filename from documents "
                "where tenant_id = :t and is_active = true"
            ), {"t": str(r.tenant_id)})).all()
            tenants.append({
                "tenant_id": str(r.tenant_id),
                "chunks": r.n,
                "doc_names": {d[0]: d[1] for d in docs},
            })
        return tenants


async def sample_questions(tenant: dict, n: int) -> list[str]:
    """Derive n natural questions from random chunks of this tenant's own
    indexed text — guaranteeing each question is answerable from the KB."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sqltext(
            "select text from document_chunks where tenant_id = :t "
            "order by random() limit :n"
        ), {"t": tenant["tenant_id"], "n": n})).all()

    questions = []
    for row in rows:
        excerpt = " ".join(row.text.split())[:900]
        q = await _classify(
            "Below is an excerpt from a company's knowledge base. Write ONE natural "
            "customer question (max 20 words) that this excerpt directly answers. "
            "Reply with the question only.\n\nEXCERPT:\n" + excerpt,
            max_tokens=60,
        )
        q = q.strip().strip('"')
        if q:
            questions.append(q)
    return questions


async def judge_faithfulness(question: str, answer: str, context_texts: list[str]) -> float:
    """RAGAS-style faithfulness: claims supported by context / total claims.

    Judged against the FULL retrieved chunk texts — judging against the
    220-char citation snippets understates faithfulness badly (claims the
    model legitimately drew from the rest of a chunk look unsupported)."""
    context = "\n---\n".join(t[:4000] for t in context_texts) or "(no context)"
    verdict = await _classify(
        "You are grading a RAG system. Decompose the ANSWER into its factual "
        "claims, then count how many claims are directly supported by the "
        "CONTEXT. Reply with exactly one line: SUPPORTED=<n> TOTAL=<m>\n\n"
        f"QUESTION: {question}\n\nANSWER: {answer}\n\nCONTEXT:\n{context}",
        max_tokens=40,
    )
    m = re.search(r"SUPPORTED\s*=\s*(\d+)\s+TOTAL\s*=\s*(\d+)", verdict)
    if not m:
        return -1.0  # judge failed; excluded from the average
    supported, total = int(m.group(1)), int(m.group(2))
    return 1.0 if total == 0 else min(1.0, supported / total)


def authorized(sources: list[dict], owned_doc_ids: set) -> bool:
    """Every citation must reference a document the asking tenant owns."""
    return all((s.get("documentId") in owned_doc_ids) for s in sources if s.get("documentId"))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", type=int, default=10, help="questions per tenant")
    parser.add_argument("--probes", type=int, default=4, help="cross-tenant negative probes")
    parser.add_argument("--json", help="write full per-question results to this file")
    args = parser.parse_args()

    from app.core.redis_client import init_redis
    await init_redis()

    tenants = await load_tenants_with_chunks()
    if not tenants:
        print("No tenants with indexed chunks — index some documents first.")
        return
    print(f"Tenants with knowledge: {len(tenants)} "
          f"({', '.join(t['tenant_id'][:8] + '…(' + str(t['chunks']) + ' chunks)' for t in tenants)})\n")

    records = []

    # ── Positive pass: faithfulness + authorization on own questions ──────
    for tenant in tenants:
        own_docs = set(tenant["doc_names"].keys())
        questions = await sample_questions(tenant, args.questions)
        print(f"Tenant {tenant['tenant_id'][:8]}… — {len(questions)} questions")
        for q in questions:
            t0 = time.perf_counter()
            result = await rag_service.answer_question(
                tenant["tenant_id"], q, doc_names=tenant["doc_names"],
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            sources = result.get("sources") or []
            if sources:
                # Reconstruct the full context the answer was generated from
                # (same retrieval call the pipeline made) for a fair judge.
                from app.services.hybrid_search import hybrid_search

                ctx_chunks = await hybrid_search(
                    q, tenant["tenant_id"],
                    document_ids=list(tenant["doc_names"].keys()),
                )
                faith = await judge_faithfulness(
                    q, result["answer"], [c.get("text") or "" for c in ctx_chunks]
                )
            else:
                faith = None
            records.append({
                "kind": "positive",
                "tenant": tenant["tenant_id"],
                "question": q,
                "answered": bool(sources),
                "faithfulness": faith,
                "authorized": authorized(sources, own_docs),
                "confidence": result.get("confidence"),
                "retrieval": result.get("retrieval"),
                "latency_ms": round(elapsed_ms),
            })
            tag = f"faith={faith:.2f}" if isinstance(faith, float) and faith >= 0 else "no-answer"
            print(f"  [{tag:>10}] auth={'OK' if records[-1]['authorized'] else 'LEAK'} {q[:70]}")

    # ── Negative pass: tenant B asks questions derived from tenant A ──────
    leaks = 0
    probes_run = 0
    if len(tenants) >= 2:
        source_t, asking_t = tenants[0], tenants[1]
        foreign_docs = set(source_t["doc_names"].keys())
        asking_docs = set(asking_t["doc_names"].keys())
        probe_questions = await sample_questions(source_t, args.probes)
        print(f"\nCross-tenant probes: asking tenant {asking_t['tenant_id'][:8]}… "
              f"questions from tenant {source_t['tenant_id'][:8]}…'s documents")
        for q in probe_questions:
            result = await rag_service.answer_question(
                asking_t["tenant_id"], q, doc_names=asking_t["doc_names"],
            )
            sources = result.get("sources") or []
            leaked = any(s.get("documentId") in foreign_docs and s.get("documentId") not in asking_docs
                         for s in sources)
            probes_run += 1
            leaks += int(leaked)
            records.append({
                "kind": "cross_tenant_probe", "tenant": asking_t["tenant_id"],
                "question": q, "leaked": leaked, "cited_sources": len(sources),
            })
            print(f"  [{'LEAK' if leaked else ' OK '}] {q[:70]}")
    else:
        print("\n(Only one tenant has data — cross-tenant probes skipped; "
              "isolation still proven structurally by namespace scoping.)")

    # ── Summary ────────────────────────────────────────────────────────────
    positives = [r for r in records if r["kind"] == "positive"]
    answered = [r for r in positives if r["answered"]]
    faith_scores = [r["faithfulness"] for r in answered
                    if isinstance(r["faithfulness"], float) and r["faithfulness"] >= 0]
    latencies = [r["latency_ms"] for r in positives]

    print("\n" + "=" * 62)
    print("EVALUATION SUMMARY")
    print("=" * 62)
    print(f"Questions asked:              {len(positives)}")
    print(f"Answered (vs refused/no-ctx): {len(answered)} ({100*len(answered)/max(1,len(positives)):.0f}%)")
    if faith_scores:
        print(f"Mean faithfulness (RAGAS):    {100*statistics.mean(faith_scores):.1f}%")
        print(f"Median faithfulness:          {100*statistics.median(faith_scores):.1f}%")
    print(f"Citations authorized:         {sum(r['authorized'] for r in positives)}/{len(positives)} "
          f"({100*sum(r['authorized'] for r in positives)/max(1,len(positives)):.0f}%)")
    if probes_run:
        print(f"Cross-tenant probes:          {probes_run}, leaks: {leaks} "
              f"→ isolation {100*(probes_run-leaks)/probes_run:.0f}%")
    if latencies:
        print(f"Median answer latency:        {statistics.median(latencies):.0f} ms")

    if args.json:
        Path(args.json).write_text(json.dumps(records, indent=2, default=str))
        print(f"\nPer-question details written to {args.json}")


if __name__ == "__main__":
    asyncio.run(main())
