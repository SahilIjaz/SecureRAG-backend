"""Ablation and gate-threshold study for Chapter 6.

Part A — retrieval ablation (no generation): Recall@5 and MRR for
    dense-only, keyword-only, hybrid (RRF), hybrid + reranker,
  on two question styles written from a known gold chunk:
    lexical      – may reuse the excerpt's words
    paraphrased  – instructed to avoid the excerpt's distinctive words
                   (the harder, more realistic case)

Part B — confidence-gate study: every question is answered once with the gate
  disabled (config=None) and its pre-generation confidence is recorded; the gate
  at any threshold T is then simulated offline (answer only if conf >= T + band;
  band = RAG_CLARIFY_BAND, or 0 for "no clarify mode"). Question sets:
    answerable    – from gold chunks (lexical + paraphrased)
    near-miss     – same topic as a chunk but asking for a detail it does NOT give
    off-topic     – generic questions
    cross-tenant  – tenant A's questions asked as tenant B
  An LLM judge marks whether a generated answer ASSERTS an answer (vs. declines),
  so "unsupported-answer rate" = answered by the gate AND asserted.

Usage: PYTHONPATH=. python -u scripts/rag_ablation.py --per-tenant 20 --json out.json
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import text as sqltext  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import AsyncSessionLocal  # noqa: E402
from app.services import hybrid_search as hs  # noqa: E402
import evaluate_rag as ev  # noqa: E402

BAND = settings.RAG_CLARIFY_BAND
CLAUDE_CLASSIFIER = "claude-haiku-5-5"


def use_claude():
    """Gemini's quota was exhausted on the day of this run, so question generation,
    judging and answering are routed to Anthropic instead (recorded in the chapter)."""
    from app.services.llm import router as llm_router
    from app.services import rag_service

    async def _classify(prompt, max_tokens=60, attempts=6):
        # Haiku sometimes returns an empty text block when max_tokens is tiny; give it room and retry on empty.
        for i in range(attempts):
            try:
                r = await llm_router.classify(prompt, provider_name="anthropic", model=CLAUDE_CLASSIFIER, max_tokens=max(max_tokens, 400))
                text = (r.text or "").strip()
                if text:
                    return text
            except Exception:
                if i == attempts - 1:
                    raise
            await asyncio.sleep(2 * (i + 1))
        return ""

    async def _answer(tenant_id, question, doc_names, attempts=4):
        for i in range(attempts):
            try:
                return await rag_service.answer_question(tenant_id, question, doc_names=doc_names, tenant_provider="anthropic")
            except Exception:
                if i == attempts - 1:
                    raise
                await asyncio.sleep(4 * (i + 1))

    ev._classify = _classify
    ev._answer = _answer


async def sample_chunks(tenant_id, n):
    async with AsyncSessionLocal() as db:
        return (await db.execute(sqltext(
            "select chunk_id, document_id::text as doc, text from document_chunks "
            "where tenant_id=:t order by random() limit :n"), {"t": tenant_id, "n": n})).all()


async def gen(prompt, excerpt):
    q = await ev._classify(prompt + "\n\nEXCERPT:\n" + " ".join(excerpt.split())[:900], max_tokens=70)
    return q.strip().strip('"')


P_LEX = "Write ONE natural customer question (max 20 words) that this excerpt directly answers. Reply with the question only."
P_PARA = ("Write ONE natural customer question (max 20 words) that this excerpt directly answers, "
          "but do NOT reuse its distinctive words or phrases; paraphrase the way a customer who has never "
          "seen the document would ask. Reply with the question only.")
P_NEAR = ("Write ONE natural customer question (max 20 words) on the same topic as this excerpt that sounds "
          "answerable from it, but asks for a specific detail the excerpt does NOT state. Reply with the question only.")


async def asserts_answer(question, answer):
    out = await ev._classify(
        "Reply with only 1 if the ANSWER states specific facts as if it were answering the QUESTION; "
        "reply 0 if it declines, says it does not know, or asks for clarification.\n\nQUESTION: "
        + question + "\n\nANSWER:\n" + answer, max_tokens=5)
    return out.strip().startswith("1")


async def retrieve(mode, q, tenant_id, docs):
    if mode == "dense":
        return await hs._vector_search(q, tenant_id, 5, docs)
    if mode == "keyword":
        return await hs._keyword_search(q, tenant_id, 5, docs)
    old = settings.RAG_RERANK_ENABLED
    settings.RAG_RERANK_ENABLED = (mode == "hybrid_rerank")
    try:
        return await hs.hybrid_search(q, tenant_id, top_k=5, document_ids=docs)
    finally:
        settings.RAG_RERANK_ENABLED = old


async def safe_answer(tenant_id, q, doc_names):
    """Answer with the gate off; None if the provider keeps failing (the question is then skipped, not scored)."""
    try:
        return await ev._answer(tenant_id, q, doc_names)
    except Exception as e:  # provider quota / outage
        print(f"    SKIPPED (provider error): {str(e)[:80]}", flush=True)
        return None


PARTIAL = None


def save_partial(retr, gen_recs):
    if PARTIAL:
        Path(PARTIAL).write_text(json.dumps({"retrieval_raw": retr, "records": gen_recs}), encoding="utf-8")


def norm_key(doc_id, chunk_id):
    """Pinecone returns chunk_id as a float (0.0), Postgres stores text ("0"), and chunk numbers
    repeat across documents - so identity is (document_id, integer chunk number)."""
    try:
        return (str(doc_id), str(int(float(chunk_id))))
    except (TypeError, ValueError):
        return (str(doc_id), str(chunk_id))


def conf_of(res):
    return int((res.get("retrieval") or {}).get("pregenConfidence", 0))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-tenant", type=int, default=20)
    ap.add_argument("--json")
    ap.add_argument("--retrieval-only", action="store_true")
    ap.add_argument("--near", type=int, default=20)
    ap.add_argument("--offtopic", type=int, default=10)
    ap.add_argument("--cross", type=int, default=10)
    args = ap.parse_args()
    global PARTIAL
    use_claude()
    PARTIAL = (args.json + ".partial") if args.json else None
    tenants = await ev.load_tenants_with_chunks()
    modes = ["dense", "keyword", "hybrid", "hybrid_rerank"]
    retr = {m: {"lexical": [], "paraphrased": []} for m in modes}   # (hit, rr)
    gen_recs = []

    for t in tenants:
        docs = list(t["doc_names"])
        rows = await sample_chunks(t["tenant_id"], args.per_tenant)
        print(f"tenant {t['tenant_id'][:8]}: {len(rows)} chunks", flush=True)
        for row in rows:
            for style, prompt in (("lexical", P_LEX), ("paraphrased", P_PARA)):
                q = await gen(prompt, row.text)
                if not q:
                    continue
                for m in modes:
                    got = await retrieve(m, q, t["tenant_id"], docs)
                    ids = [norm_key(c.get("document_id"), c.get("chunk_id")) for c in got[:5]]
                    gold = norm_key(row.doc, row.chunk_id)
                    hit = gold in ids
                    retr[m][style].append((1.0 if hit else 0.0, 1.0 / (ids.index(gold) + 1) if hit else 0.0))
                if args.retrieval_only:
                    continue
                res = await safe_answer(t["tenant_id"], q, t["doc_names"])
                if res is None:
                    continue
                correct = None
                if res.get("sources"):
                    correct = (await ev._classify(
                        "Reply with only 1 if the ANSWER is consistent with GOLD and answers the QUESTION, else 0.\n\nQUESTION: "
                        + q + "\n\nGOLD:\n" + " ".join(row.text.split())[:1500] + "\n\nANSWER:\n" + res["answer"], max_tokens=5)
                    ).strip().startswith("1")
                gen_recs.append({"set": "answerable_" + style, "q": q, "conf": conf_of(res), "asserted": True, "correct": correct})
                print(f"  {style[:4]} conf={conf_of(res)} {q[:60]!r}", flush=True); save_partial(retr, gen_recs)
        if t is tenants[0] and not args.retrieval_only:
            for row in await sample_chunks(t["tenant_id"], args.near):
                q = await gen(P_NEAR, row.text)
                if not q:
                    continue
                res = await safe_answer(t["tenant_id"], q, t["doc_names"])
                if res is None:
                    continue
                gen_recs.append({"set": "near_miss", "q": q, "conf": conf_of(res),
                                 "asserted": await asserts_answer(q, res.get("answer") or "")})
                print(f"  near conf={conf_of(res)} {q[:60]!r}", flush=True); save_partial(retr, gen_recs)

    for t in ([] if args.retrieval_only else tenants):
        for q in ev.OFF_TOPIC_QUESTIONS[:args.offtopic]:
            res = await safe_answer(t["tenant_id"], q, t["doc_names"])
            if res is None:
                continue
            gen_recs.append({"set": "off_topic", "q": q, "conf": conf_of(res),
                             "asserted": await asserts_answer(q, res.get("answer") or "")})
    if len(tenants) >= 2 and not args.retrieval_only:
        a, b = tenants[0], tenants[1]
        for row in await sample_chunks(a["tenant_id"], args.cross):
            q = await gen(P_LEX, row.text)
            res = await safe_answer(b["tenant_id"], q, b["doc_names"])
            if res is None:
                continue
            gen_recs.append({"set": "cross_tenant", "q": q, "conf": conf_of(res),
                             "asserted": await asserts_answer(q, res.get("answer") or "")})
    print("generation pass done", flush=True)

    # ── summaries ──
    ablation = {}
    for m in modes:
        ablation[m] = {}
        for style, vals in retr[m].items():
            n = len(vals)
            ablation[m][style] = {"n": n, "recall_at_5": sum(v[0] for v in vals) / n if n else None,
                                  "mrr": sum(v[1] for v in vals) / n if n else None}

    def sweep(band):
        if not gen_recs:
            return [], {}
        table = []
        ans = [r for r in gen_recs if r["set"].startswith("answerable")]
        unans = [r for r in gen_recs if not r["set"].startswith("answerable")]
        for T in range(40, 85, 5):
            ok = lambda r: r["conf"] >= T + band  # noqa: E731
            row = {"threshold": T,
                   "answerable_answered": sum(ok(r) for r in ans) / len(ans),
                   "unsupported_answer_rate": sum(1 for r in unans if ok(r) and r["asserted"]) / len(unans),
                   "unanswerable_answered": sum(ok(r) for r in unans) / len(unans)}
            for s in ("near_miss", "off_topic", "cross_tenant"):
                sub = [r for r in unans if r["set"] == s]
                row[s + "_unsupported"] = (sum(1 for r in sub if ok(r) and r["asserted"]) / len(sub)) if sub else None
            table.append(row)
        gate_off = {"answerable_answered": 1.0,
                    "unsupported_answer_rate": sum(1 for r in unans if r["asserted"]) / len(unans)}
        return table, gate_off

    out = {"n": {s: sum(1 for r in gen_recs if r["set"] == s) for s in {r["set"] for r in gen_recs}},
           "ablation": ablation, "band": BAND,
           "sweep_production_band": sweep(BAND)[0], "sweep_no_clarify": sweep(0)[0], "gate_off": sweep(BAND)[1],
           "answerable_correct_when_answered": (lambda xs: sum(xs) / len(xs) if xs else None)(
               [r["correct"] for r in gen_recs if r["set"].startswith("answerable") and r["correct"] is not None])}
    print(json.dumps(out, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps({"summary": out, "records": gen_recs}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
