"""Quality and performance metrics for Chapter 6, measured on real tenants.

Quality:  answer correctness, faithfulness, citation accuracy, Recall@5,
          abstention rate (on unanswerable questions), conflict-resolution
          accuracy (from the pytest conflict suite — see the chapter).
Performance: P50 / P95 answer latency, mean prompt tokens, mean USD per answer.

Method: for each tenant with indexed knowledge, sample real chunks, have the
cheap model write one question each chunk answers (so the gold chunk and gold
text are known), run the production answer path, and score:
  * Recall@5      – the gold chunk is among the top-5 chunks hybrid_search returns
  * citation acc. – the answer cites the gold chunk's document
  * correctness   – LLM judge: does the answer agree with the gold excerpt (0/1)
  * faithfulness  – RAGAS-style (reuses evaluate_rag.judge_faithfulness)
  * abstention    – on off-topic and cross-tenant questions, the pre-generation
                    gate (confidence < threshold) refuses or asks to clarify
                    instead of answering
Usage:  PYTHONPATH=. python scripts/rag_quality_metrics.py --per-tenant 15 --json out.json
"""
import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import text as sqltext  # noqa: E402

from app.database import AsyncSessionLocal  # noqa: E402
from app.services import rag_service  # noqa: E402
from app.services.hybrid_search import hybrid_search  # noqa: E402
from app.services.llm import pricing  # noqa: E402
import evaluate_rag as ev  # noqa: E402

THRESHOLD = 60          # default confidenceThreshold
CLARIFY_BAND = 15       # RAG_CLARIFY_BAND


def pct(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


async def sample_chunks(tenant_id: str, n: int):
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sqltext(
            "select chunk_id, document_id::text as doc, text from document_chunks "
            "where tenant_id = :t order by random() limit :n"), {"t": tenant_id, "n": n})).all()
    return rows


async def judge_correct(question: str, answer: str, gold: str) -> float:
    out = await ev._classify(
        "You grade a support chatbot. GOLD is the excerpt that answers the QUESTION. "
        "Reply with only 1 if the ANSWER is consistent with GOLD and actually answers the question, "
        "otherwise reply 0.\n\nQUESTION: " + question + "\n\nGOLD:\n" + " ".join(gold.split())[:1500]
        + "\n\nANSWER:\n" + answer, max_tokens=5)
    return 1.0 if out.strip().startswith("1") else 0.0


def decision(conf: int) -> str:
    return "refuse" if conf < THRESHOLD else ("clarify" if conf < THRESHOLD + CLARIFY_BAND else "answer")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-tenant", type=int, default=15)
    ap.add_argument("--json")
    args = ap.parse_args()

    tenants = await ev.load_tenants_with_chunks()
    rec, lat, ptoks, cost = [], [], [], []
    for t in tenants:
        own = set(t["doc_names"])
        for row in await sample_chunks(t["tenant_id"], args.per_tenant):
            q = await ev._classify(
                "Below is an excerpt from a company's knowledge base. Write ONE natural customer "
                "question (max 20 words) that this excerpt directly answers. Reply with the question only.\n\n"
                "EXCERPT:\n" + " ".join(row.text.split())[:900], max_tokens=60)
            q = q.strip().strip('"')
            if not q:
                continue
            t0 = time.perf_counter()
            res = await ev._answer(t["tenant_id"], q, t["doc_names"])
            ms = (time.perf_counter() - t0) * 1000
            top5 = await hybrid_search(q, t["tenant_id"], top_k=5, document_ids=list(own))
            recall5 = any(c.get("chunk_id") == row.chunk_id for c in top5)
            sources = res.get("sources") or []
            cited_gold = any(s.get("documentId") == row.doc for s in sources)
            answered = bool(sources)
            correct = faith = None
            if answered:
                correct = await judge_correct(q, res["answer"], row.text)
                faith = await ev.judge_faithfulness(q, res["answer"], [c.get("text") or "" for c in top5])
            u = res.get("usage")
            if u is not None and getattr(u, "prompt_tokens", None) is not None:
                ptoks.append(u.prompt_tokens)
                c = pricing.estimate_cost_usd(u.provider, u.model, u.prompt_tokens, u.completion_tokens)
                if c is not None:
                    cost.append(float(c))
            lat.append(ms)
            rec.append({"kind": "answerable", "q": q, "recall5": recall5, "cited_gold_doc": cited_gold,
                        "answered": answered, "correct": correct, "faithfulness": faith,
                        "authorized": ev.authorized(sources, own), "latency_ms": round(ms),
                        "decision": decision((res.get("retrieval") or {}).get("pregenConfidence", 0))})
            print(f"  {q[:70]!r} R@5={recall5} cite={cited_gold} corr={correct} faith={faith} {ms:.0f}ms")

    # Unanswerable: off-topic + cross-tenant probes
    neg = []
    for t in tenants:
        for q in ev.OFF_TOPIC_QUESTIONS:
            neg.append(("off_topic", t, q))
    if len(tenants) >= 2:
        a, b = tenants[0], tenants[1]
        for row in await sample_chunks(a["tenant_id"], 10):
            q = await ev._classify("Write ONE natural customer question (max 20 words) that this excerpt answers. "
                                   "Reply with the question only.\n\n" + " ".join(row.text.split())[:900], max_tokens=60)
            neg.append(("cross_tenant", b, q.strip().strip('"')))
    for kind, t, q in neg:
        res = await ev._answer(t["tenant_id"], q, t["doc_names"])
        conf = (res.get("retrieval") or {}).get("pregenConfidence", 0)
        rec.append({"kind": kind, "q": q, "decision": decision(conf), "pregen_confidence": conf,
                    "leaked": not ev.authorized(res.get("sources") or [], set(t["doc_names"]))})

    ans = [r for r in rec if r["kind"] == "answerable"]
    neg_r = [r for r in rec if r["kind"] != "answerable"]
    mean = lambda xs: (sum(xs) / len(xs)) if xs else None  # noqa: E731
    lat_s = sorted(lat)
    out = {
        "answerable_n": len(ans), "unanswerable_n": len(neg_r),
        "answer_correctness": mean([r["correct"] for r in ans if r["correct"] is not None]),
        "faithfulness": mean([r["faithfulness"] for r in ans if r["faithfulness"] is not None]),
        "citation_accuracy": mean([1.0 if r["cited_gold_doc"] else 0.0 for r in ans if r["answered"]]),
        "recall_at_5": mean([1.0 if r["recall5"] else 0.0 for r in ans]),
        "abstention_rate_unanswerable": mean([0.0 if r["decision"] == "answer" else 1.0 for r in neg_r]),
        "false_abstention_rate_answerable": mean([0.0 if r["decision"] == "answer" else 1.0 for r in ans]),
        "cross_tenant_leaks": sum(1 for r in neg_r if r.get("leaked")),
        "latency_p50_ms": pct(lat_s, 0.5), "latency_p95_ms": pct(lat_s, 0.95),
        "mean_prompt_tokens": mean(ptoks), "mean_usd_per_answer": mean(cost),
        "usd_priced_answers": len(cost),
    }
    print(json.dumps(out, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps({"summary": out, "records": rec}, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
