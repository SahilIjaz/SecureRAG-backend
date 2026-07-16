# SecureRAG — Project Details (One-Liner Reference)

## What It Is

- SecureRAG is a multi-tenant B2B SaaS platform for document-based AI question answering.
- Companies sign up, create an isolated workspace, upload documents, and ask questions answered by an LLM using only their own documents.
- The pics folder contains Nexus dashboard UI screenshots used as the frontend design reference.

## Tech Stack

- API framework: FastAPI 0.115 with Uvicorn, fully async.
- Validation: Pydantic v2 at request/response boundaries.
- Database: PostgreSQL with SQLAlchemy 2.0 async ORM and asyncpg driver.
- Vector database: Pinecone, index name `securerag-documents`.
- LLM: Claude 3.5 Sonnet (`claude-3-5-sonnet-20241022`) for answer generation.
- Tokenizer: tiktoken with `gpt-3.5-turbo` encoding for chunk sizing.
- File storage: Cloudinary (only `public_id` and `secure_url` stored in DB).
- Web scraping: Crawl4AI with 30s timeout, converts websites to PDF documents.
- Email delivery: SMTP (Gmail) or Brevo API for OTPs.
- Authentication: JWT HS256 via python-jose, bcrypt via passlib.
- Rate limiting: slowapi, per-IP based.

## Database Schema (8 Tables)

- `tenants`: workspace_name, unique auto-generated slug, business_category, employee_count_range.
- `users`: unique email, nullable password_hash (for Google users), auth_provider, is_email_verified, FK tenant_id.
- Each tenant has exactly ONE user (1:1 relationship).
- `subscriptions`: plan_name (free/pro/pro_plus), billing_cycle, status, expires_at.
- `tenant_quotas`: max_documents, max_file_size_mb, max_questions_per_month (-1 means unlimited).
- `usage_counts`: one row per month tracking documents_count, storage_used_mb, questions_asked.
- `email_verifications`: bcrypt-hashed 4-digit OTPs with purpose, 10-min expiry, single-use flag.
- `documents`: Cloudinary refs, source (uploaded/sample/scraped), status lifecycle, soft-delete via is_active.
- `sample_documents`: platform starter docs filtered by business_category.
- Every query filters by tenant_id — no cross-tenant data access is possible.

## Authentication Tokens (4 Types)

- Onboarding token: claim `purpose=onboarding`, valid 1 hour, used only for `/onboarding/complete`.
- Access token: claim `type=access`, valid 30 minutes, used for all protected endpoints.
- Refresh token: claim `type=refresh`, valid 7 days, exchanged at `/refresh`.
- Reset token: claim `purpose=password_reset`, valid 15 minutes, legacy reset flow only.
- All tokens are HS256-signed JWTs using SECRET_KEY.

## Signup Flow

- `POST /signup` accepts companyName, email, password.
- Password is bcrypt-hashed in a thread executor to avoid blocking the event loop.
- A placeholder tenant is created with workspace_name `__pending__` and slug `pending-<8 hex>`.
- A 4-digit OTP is generated, bcrypt-hashed, stored, and emailed via background asyncio task.
- `POST /verify-email` checks OTP hash, expiry, and single-use flag, then issues the onboarding token.
- `POST /onboarding/complete` fills tenant info, creates subscription + quota + usage rows in one atomic transaction, and returns access/refresh tokens.
- Signin returns an onboarding token instead of access tokens if tenant.business_category is still null (half-onboarded users get routed back).

## Password Reset Flow

- `POST /forgot-password` sends an OTP and returns the same message whether the account exists or not (prevents email enumeration).
- `POST /reset-password` accepts email + otp + newPassword in a single call with no bearer token required.

## Document Ingestion Pipeline

- Step 1: Validate MIME type, per-file size vs quota, and document count vs quota before any external calls.
- Step 2: Upload raw bytes to Cloudinary.
- Step 3: Create Document row with status=pending and increment usage counters in one commit.
- Step 4: Extract text page-by-page with PyPDF2, joined with `--- Page N ---` markers.
- Step 5: Chunk text into 500-token windows with 50-token overlap (window advances 450 tokens per step).
- A 50-page PDF (~25,000 tokens) produces roughly 55 chunks.
- Step 6: Each chunk becomes one 1,536-dimensional embedding vector.
- Step 7: Vectors are upserted to Pinecone in batches of 100.
- Vector ID scheme: `{tenant_id}#{document_id}#{chunk_id}`.
- Vector metadata: tenant_id, document_id, chunk_id, sequence, first 200 chars of text, token_count.
- Step 8: Document status flips to `ready` on success or `failed` on error (logged, never crashes).

## RAG Query Pipeline

- Step 1: The user query is embedded into the same 1,536-dim vector space.
- Step 2: Pinecone similarity search runs with top_k=5 and metadata filter `tenant_id = current tenant`.
- Tenant isolation at the vector layer uses metadata filtering, not namespaces.
- Step 3: The 5 matched chunk texts are joined into a CONTEXT block inside the prompt.
- Step 4: Claude 3.5 Sonnet generates the answer with max 1,024 output tokens.
- Step 5: questions_asked is incremented and requests are rejected with 403 once the monthly quota is hit.
- Response contains: answer, sources, and model name.

## ⚠️ Known Limitations (From Actual Code)

- The embedding function is a DEV PLACEHOLDER: it MD5-hashes text and converts hex pairs to floats.
- Only the first 16 of 1,536 dimensions carry values; the remaining 1,520 are zero-padded.
- Similarity search currently matches on hash similarity, not semantic meaning.
- Production fix: swap in OpenAI `text-embedding-3-small` (natively 1,536-dim), sentence-transformers, or Cohere.
- Pinecone metadata stores only the first 200 characters of each chunk.
- Claude therefore receives at most 5 × 200 ≈ 1,000 characters of context per question.
- Fix: store full chunk text in metadata (~40KB limit per vector) or fetch full text from Postgres by chunk_id after search.

## Security

- Passwords and OTPs are bcrypt-hashed (work factor 12).
- Password complexity enforced at Pydantic boundary: 8+ chars, uppercase, lowercase, digit.
- Email enumeration prevented on forgot-password with a uniform response message.
- Rate limits: 3/min on signup, 5/min on verify-email, per IP.
- CORS is restricted to configured origins.
- SSRF-conscious scraping via a dedicated IP validator module.
- Fernet key configured for file encryption.
- GDPR-style cleanup helper `clear_tenant_data` wipes all of a tenant's vectors from Pinecone.

## Plan Quotas

- Free: 10 documents, 15MB per file, 50 questions/month.
- Pro: 100 documents, 50MB per file, unlimited questions.
- Pro Plus: everything unlimited (-1 sentinel values).
- Paid plan expiry: +30 days monthly, +365 days annual.

## Key Numbers at a Glance

- Embedding dimensions: 1,536.
- Chunk size: 500 tokens.
- Chunk overlap: 50 tokens.
- Vectors per document: ~1 per 450 tokens of text.
- Retrieval: top-5 chunks by cosine similarity, tenant-filtered.
- LLM max answer tokens: 1,024.
- OTP: 4 digits, 10-minute expiry, bcrypt-hashed, single-use.
- Token lifetimes: access 30m, refresh 7d, onboarding 1h, reset 15m.
- Pinecone upsert batch size: 100 vectors.

## Bottom Line

- The platform plumbing (auth, multi-tenancy, quotas, document lifecycle, Pinecone, Claude) is production-shaped.
- The one blocker for real semantic search quality is replacing the placeholder embedding function (~20-line change since the 1,536-dim interface is already correct).
- The second quick win is un-truncating the chunk text sent to Claude.
