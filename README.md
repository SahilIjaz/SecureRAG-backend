# Nexus Backend

FastAPI backend for **Nexus** (also referenced internally as SecureRAG++) — a
multi-tenant Retrieval-Augmented Generation platform that lets a business
train an AI support chatbot on its own documents, URLs, and FAQs, deploy it
as an embeddable widget, and manage the whole thing (knowledge base,
conversations, billing) from a dashboard.

## Stack

- **FastAPI** + **async SQLAlchemy** on **Postgres** (Neon)
- **Pinecone** for vector search (hosted `multilingual-e5-large` embeddings)
- **Google Gemini** (`gemini-flash-latest`, with a fallback chain) for answer generation
- **Stripe** for billing — trial subscriptions, plan changes, webhooks
- **Cloudinary** for uploaded-file storage, **Crawl4AI** for URL scraping
  (`app/core/scraper.py`: SSRF-checked redirects, direct PDF links, clean
  text, and Gemini-vision OCR of page images and scanned PDFs via
  `app/core/ocr.py`)
- JWT auth (access tokens with revocation support), OTP email verification
- No Alembic — schema changes ship as idempotent `ALTER TABLE ... IF NOT EXISTS`
  statements run at startup (see `app/main.py`)

## Project structure

```
app/
  api/
    frontend/        # /api/* routes matching the dashboard 1:1 (auth, onboarding,
                      # chatbot, conversations, knowledge, settings, notifications)
    public/           # /api/public/widget/* — the embeddable chat widget's own API,
                      # separate auth model (widget API key, not JWT)
    billing_webhook.py  # POST /api/billing/webhook — Stripe webhook, signature-verified
  core/               # security (JWT/OTP), email sending, rate limiting, background jobs
  models/             # SQLAlchemy models (Tenant, User, Subscription, TenantQuota,
                      # Document, Conversation, Notification, ...)
  schemas/            # Pydantic wire-format schemas for the frontend-compat API
  services/           # business logic — auth, RAG (retrieval + generation), document
                      # indexing, Stripe billing, notifications, conversation lifecycle
  config.py           # Settings (env-driven), database.py, main.py (app + startup)
scripts/              # one-off maintenance scripts, e.g. setup_stripe_products.py
migrations/           # reference SQL (init.sql) — not applied automatically
```

## Getting started

```bash
python -m venv venv
venv\Scripts\activate        # Windows; use `source venv/bin/activate` on macOS/Linux
pip install -r requirements.txt

copy .env.example .env       # then fill in the real values (see below)
uvicorn app.main:app --reload --port 8000
```

The API comes up on `http://localhost:8000` — interactive docs at `/docs`.
On startup it idempotently applies any pending schema changes and does a
Gemini model-reachability check; both log to the console.

### Environment variables

Copy `.env.example` to `.env` and fill in real values. At minimum you need:

| Variable | What it's for |
|---|---|
| `DATABASE_URL` | Postgres connection string (Neon or any Postgres works) |
| `SECRET_KEY` | JWT signing secret |
| `GEMINI_API_KEY` | Google AI Studio key — answer generation |
| `PINECONE_API_KEY` | Vector search |
| `CLOUDINARY_*` | File storage |
| `STRIPE_SECRET_KEY` | Billing — see below |
| `SMTP_*` or `BREVO_API_KEY` | OTP / notification emails |

Everything else has a sane default — see `app/config.py` for the full list.
Scraping knobs worth knowing: `CRAWL4AI_TIMEOUT` (seconds per network step),
`CRAWL4AI_MAX_CONTENT_SIZE_MB`, and the OCR group — `SCRAPE_OCR_ENABLED`
(default on), `GEMINI_OCR_MODEL`, `SCRAPE_OCR_MAX_IMAGES` (per page, also the
cost cap; each image is one Gemini call billed to the tenant's wallet, free
during the signup trial).

### Stripe setup (one-time)

```bash
python -m scripts.setup_stripe_products
```

Idempotently creates the Starter/Growth/Business Products and Prices in your
Stripe account and prints the three Price IDs to paste into `.env` as
`STRIPE_PRICE_STARTER` / `STRIPE_PRICE_GROWTH` / `STRIPE_PRICE_BUSINESS`.

For webhooks locally, run the Stripe CLI alongside the server:

```bash
stripe listen --forward-to localhost:8000/api/billing/webhook
```

and put the printed `whsec_...` into `.env` as `STRIPE_WEBHOOK_SECRET`.

## Notes on the architecture

- **Multi-tenancy** is row-level: every tenant-owned table carries a
  `tenant_id` foreign key, enforced in every query — not schema- or
  database-per-tenant.
- **Plans**: the `PlanName` enum (`free`/`pro`/`pro_plus`) stays as originally
  named at the database level; `app/api/frontend/helpers.py` is the single
  place that maps it to the user-facing plan names (Starter/Growth/Business)
  and their real costed quotas.
- **Billing state** is written authoritatively by the Stripe webhook handler,
  not by the client confirming payment — see `app/services/stripe_service.py`.
- **Two auth surfaces**: dashboard requests use a JWT Bearer token
  (`get_current_user`); the public widget uses a separate per-tenant API key
  and has no access to any dashboard-authenticated route.

## Confidence, conflict detection & evaluation

Every reply is confidence-gated (see `app/services/confidence.py` and the
gate in `app/services/rag_service.py`). Two signals: the LLM's self-reported
`CONFIDENCE: NN` line, and a computed retrieval score —
`100·(0.5·similarity + 0.3·coverage + 0.2·(1−entropy))` over the retrieved
chunks' relevance scores — used as the guard when the self-report is absent.
Four outcomes: **answer** (confident), **clarify** (borderline band
`RAG_CLARIFY_BAND` above the tenant threshold → the bot asks one clarifying
question), **handoff** (below threshold, handoff enabled), **refuse** (below
threshold, handoff disabled — a weak answer is never shipped).

### Contradiction detection and evidence-based resolution

Retrieved chunks are scanned for contradictions before the context is built
(`app/services/conflict_detection.py`):

| Type       | Example                                              | Method                                                   |
|------------|------------------------------------------------------|----------------------------------------------------------|
| `numeric`  | "refunds within 7 days" vs "… within 14 days"        | exact: same subject words, compatible unit, values differ |
| `date`     | "effective 1 March 2026" vs "effective 2026-06-01"   | exact: same subject words, dates differ                   |
| `semantic` | "refunds are allowed" vs "refunds are not allowed"   | heuristic (negation / antonym about a shared subject), optionally confirmed by one cached classification call (`RAG_CONFLICT_LLM_VERIFY`) |

Two statements only count as contradicting when they are about the same
subject under compatible conditions — "7 days for digital products" vs
"30 days for physical products" is not a conflict. Near-identical statements
inside one document are reported too (`scope: intra_document`) but are
disclosure-only.

Each cross-document conflict is then **resolved deterministically**
(`app/services/conflict_resolution.py`) from admin-maintained authority
metadata on `documents` — `version`, `effective_date`, `approval_status`,
`supersedes_document_id`, `authority_rank` — set only through
`PATCH /api/knowledge/documents/{id}/authority` (admin/owner). The rules, in
order: an approved and currently effective document that explicitly
supersedes the other wins; otherwise the only approved-and-effective
document wins; between two approved documents a later effective date or a
higher admin-assigned rank decides, and contradictory evidence stays
unresolved. An unapproved document never wins, whatever its version, rank
or upload time; similarity scores and model confidence are never evidence.
Supersession cannot cross tenants (composite FK
`fk_documents_supersedes_same_tenant`) and the authority lookup is
tenant-scoped.

Resolved conflicts drop the losing statement (or the whole superseded
document) from the context and tell the model which source is current.
Unresolved ones are handled according to `RAG_UNRESOLVED_CONFLICT_ACTION`:
`disclose` (default — generate, then verify in code that both positions
appear in the reply and append them if not), `handoff`, or `refuse`. The
test-chat response carries `conflicts[]` / `conflict` for the dashboard.
Tests: `tests/test_conflicts.py`.

## Audit trail, response traceability & compliance evidence

**Audit trail** (`app/core/audit_middleware.py`, `app/services/audit_service.py`,
table `audit_logs`): every mutating dashboard/API request becomes one
append-only entry — actor (email/role from the bearer token), action
(`document.delete`, `settings.workspace.update`, …), resource (handlers add
the label via `request.state.audit`), outcome + status code, IP, user agent,
request id. Authentication events are recorded explicitly with the attempted
email and failure reason (`auth.login`, `auth.logout`, `user.password.change`,
`audit.export`). Entries form a per-tenant SHA-256 hash chain
(`prev_hash` → `entry_hash`); a database trigger rejects UPDATE and any
DELETE other than the daily retention sweep (`AUDIT_RETENTION_DAYS`).
Admin API: `GET /api/audit` (filter/paginate), `/api/audit/actions`,
`/api/audit/verify` (recompute the chain), `/api/audit/compliance`
(controls + retention + integrity), `GET /api/audit/export?format=csv|json`
(owner only, itself audited). Dashboard: Settings → **Audit log**.

**Response traceability** (`app/services/trace_service.py`, table
`answer_traces`): every generated reply — real widget visitors, the public
bot page and dashboard test chats — stores the question, the answer, the
gate decision, confidence, provider/model/tokens, latency, retrieval
metrics, the retrieved chunks with similarity/rerank scores and capped
snippets, and any contradictions with their resolution. The stored bot
message links to it (`conversation_messages.trace_id`); widget conversations
now persist citations too. API: `GET /api/traces/{id}`,
`GET /api/traces?conversationId=…` (admin+). Dashboard: "Why this answer?"
on bot messages in Conversations and in the Test chatbot. Retention:
`TRACE_RETENTION_DAYS`.

Usage logging (provider, model, tokens, cost) already lives in
`llm_usage_logs` / `wallet_transactions`. Tests: `tests/test_audit.py`.

Evaluation (`scripts/evaluate_rag.py`) measures RAGAS-style faithfulness
(LLM judge counts answer claims supported by the retrieved context) plus
tenant isolation (citation authorization + cross-tenant probes):

    PYTHONPATH=. ./venv/bin/python scripts/evaluate_rag.py --questions 20 --json results.json

## Post-quantum cryptography (quantum-safe mode)

The platform's two self-owned crypto surfaces can run in a hybrid
post-quantum mode built on the NIST-standardized lattice algorithms
(`app/core/pq_crypto.py`):

- **Files at rest** — envelope encryption: each uploaded/scraped file gets a
  fresh symmetric key, which is wrapped under an **ML-KEM-768** (Kyber,
  FIPS 203) encapsulation against the platform public key. This defeats
  *harvest-now-decrypt-later*: ciphertext stolen today stays unreadable even
  to a future quantum adversary. Measured overhead ≈ 8–23 ms and +1.2 KB per
  file.
- **Widget session tokens** — an **ML-DSA-44** (Dilithium, FIPS 204)
  signature is appended to the existing HMAC JWT; verification requires both
  layers. Trade-off: ≈ 78 ms to sign, token grows from ~130 B to ~3.4 KB.

Both features are **hybrid** (classical crypto always remains underneath),
**off by default**, and **fully reversible** — the PQ envelope is detected by
a magic prefix inside the stored blob and legacy unsigned tokens stay
accepted, so flipping the flags never strands existing data or sessions.

Setup: `python scripts/generate_pq_keys.py`, paste the printed keys into
`.env`, set `PQ_FILE_ENCRYPTION_ENABLED=true` and/or
`PQ_WIDGET_TOKEN_SIGNING_ENABLED=true`. Benchmarks:
`PYTHONPATH=. python scripts/benchmark_pq.py` · Tests:
`PYTHONPATH=. pytest tests/`.
