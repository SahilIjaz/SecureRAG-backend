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
