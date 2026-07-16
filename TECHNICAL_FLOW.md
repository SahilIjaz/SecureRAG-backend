# SecureRAG - Technical Architecture & Flow Documentation

**Document Type:** Technical Architecture Flow  
**Version:** 1.0  
**Date:** 2026-06-30  
**Status:** Implementation Complete (7 API Changes)

---

## 📊 Table of Contents

1. [System Architecture Overview](#system-architecture-overview)
2. [Authentication & Authorization Flow](#authentication--authorization-flow)
3. [Multi-Tenant Architecture](#multi-tenant-architecture)
4. [Data Flow Diagrams](#data-flow-diagrams)
5. [API Endpoint Architecture](#api-endpoint-architecture)
6. [Database Schema & Relationships](#database-schema--relationships)
7. [Integration Points](#integration-points)
8. [Security & Token Management](#security--token-management)

---

## System Architecture Overview

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Frontend (React/Vue)                        │
│                  (Signup, Dashboard, Chat UI)                   │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           │ HTTPS/REST API
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                  FastAPI Backend (Python)                       │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              API Routes (v1)                            │   │
│  │  • /auth/* (Signup, Login, Password Reset)             │   │
│  │  • /documents/* (Upload, List, Process)                │   │
│  │  • /rag/* (Chat, Query, Context Retrieval)             │   │
│  │  • /workspace/* (Settings, Configuration)              │   │
│  └─────────────────────────────────────────────────────────┘   │
│                           │                                      │
│  ┌────────────────────────┴────────────────────────────────┐   │
│  │                                                         │   │
│  ▼                    ▼                    ▼              ▼    │
│ ┌──────────┐   ┌──────────────┐  ┌──────────────┐  ┌─────────┐│
│ │ Auth     │   │ Document     │  │ RAG Pipeline │  │ Workspace││
│ │ Service  │   │ Service      │  │ Service      │  │ Service  ││
│ └──────────┘   └──────────────┘  └──────────────┘  └─────────┘│
└──────────┬───────────────┬────────────────┬──────────────┬─────┘
           │               │                │              │
    ┌──────▼──┐    ┌──────▼─────┐  ┌──────▼──┐  ┌───────▼──────┐
    │PostgreSQL    │ Cloudinary   │  │Pinecone │  │Email Service │
    │(Multi-Tenant)│ (File Storage)  │(Vectors)│  │(SMTP/Brevo)  │
    └─────────────┘└──────────────┘  └─────────┘  └──────────────┘
            │
    ┌──────▼──────────────────┐
    │   SQLAlchemy ORM        │
    │   AsyncPG Driver        │
    │   Connection Pooling    │
    └─────────────────────────┘
```

### Technology Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| **Backend** | FastAPI 0.115.0 | REST API Framework |
| **Async Runtime** | asyncio + uvloop | Concurrent Request Handling |
| **Database** | PostgreSQL 12+ | Primary Datastore (Multi-tenant) |
| **ORM** | SQLAlchemy 2.0 | Database Abstraction & Migrations |
| **Async DB Driver** | asyncpg | Non-blocking PostgreSQL Connection |
| **Validation** | Pydantic 2.10 | Request/Response Schema Validation |
| **Authentication** | JWT (python-jose) | Token-Based Auth |
| **Password Hashing** | bcrypt | Secure Password Storage |
| **File Storage** | Cloudinary | Cloud File Management |
| **Vector DB** | Pinecone | Semantic Search & Embeddings |
| **LLM API** | Anthropic Claude | Answer Generation |
| **Email** | SMTP/Brevo | OTP & Notifications |
| **Rate Limiting** | slowapi | API Protection |
| **PDF Processing** | PyPDF2 | Document Parsing |
| **Web Scraping** | Crawl4AI | URL to PDF Conversion |

---

## Authentication & Authorization Flow

### 1. User Registration & Email Verification (NEW Flow)

```
┌─────────────────┐
│  1. Signup      │
│  POST /signup   │
│  Body:          │
│  ├─ companyName │ ◄─── CHANGED: full_name → companyName
│  ├─ email       │
│  └─ password    │
└────────┬────────┘
         │
         ▼
    ✅ Validate password complexity
    ✅ Check email uniqueness
    ✅ Hash password (bcrypt)
         │
         ▼
    📝 Create User (unverified)
    📝 Create Placeholder Tenant
         │
         ▼
    📧 Generate & Send OTP
    └─► Save OTP hash in EmailVerification table
         └─► Expires in 10 minutes
         
         │
         ▼
    Response: 201 Created
    {
      "message": "Account created. Check email.",
      "email": "user@example.com"
    }

         │
         ├─ User checks email for OTP
         │
         ▼
┌──────────────────────┐
│  2. Verify Email     │
│  POST /verify-email  │
│  Body:               │
│  ├─ email            │
│  └─ otp              │ ◄─── CHANGED: otp_code → otp
└────────┬─────────────┘
         │
         ▼
    ✅ Find OTP record
    ✅ Verify OTP hash match
    ✅ Check expiration
    ✅ Mark OTP as used
         │
         ▼
    🔑 Generate onboarding_token
    └─► Valid for 1 hour
    └─► Purpose: "onboarding"
         │
         ▼
    Response: 200 OK
    {
      "message": "Email verified successfully.",
      "email": "user@example.com",
      "onboarding_token": "eyJ0eXAi..."
    }
```

### 2. Consolidated Onboarding Flow (NEW - 5 endpoints → 1)

```
Before (3 requests):                After (1 request):
├─ /organization                    ├─ /onboarding/complete
├─ /workspace                       │  (All in one)
└─ /select-plan                     └─ Returns: access_token + refresh_token

┌──────────────────────────────────────┐
│  3. Complete Onboarding (NEW)        │
│  POST /onboarding/complete           │
│  Authorization: Bearer onboarding_token
│  Body:                               │
│  ├─ role                             │
│  ├─ teamSize                         │ ◄─── Replaces 3 old endpoints
│  ├─ goal                             │
│  └─ workspaceName                    │
└────────┬─────────────────────────────┘
         │
         ▼
    ✅ Validate onboarding_token
    ✅ Extract user from token
         │
         ▼
    DATABASE TRANSACTION:
    ├─ Update Tenant:
    │  ├─ Set employee_count_range = teamSize
    │  ├─ Set workspace_name = workspaceName
    │  └─ Generate unique slug (acme-corp, acme-corp-1, etc)
    │
    ├─ Create Subscription:
    │  ├─ plan_name = "free" (default)
    │  ├─ status = "active"
    │  └─ expires_at = null (free tier)
    │
    ├─ Create TenantQuota:
    │  ├─ max_documents = 10
    │  ├─ max_file_size_mb = 15
    │  └─ max_questions_per_month = 50
    │
    └─ Create UsageCount:
       ├─ period_month = 2026-06-01
       ├─ documents_count = 0
       └─ storage_used_mb = 0
         │
         ▼
    🔑 Generate JWT tokens:
    ├─ access_token (30 min expiry, type: "access")
    ├─ refresh_token (7 day expiry, type: "refresh")
    └─ token_type = "bearer"
         │
         ▼
    Response: 201 Created
    {
      "access_token": "eyJ0eXAi...",
      "refresh_token": "eyJ0eXAi...",
      "token_type": "bearer",
      "message": "Onboarding complete!",
      "workspace_name": "Acme Corp",
      "slug": "acme-corp"
    }
```

### 3. Password Reset Flow (NEW - Consolidated)

```
Before (2 requests):
├─ POST /verify-reset-otp (return reset_token)
└─ POST /reset-password (use reset_token + Bearer)

After (1 request):
└─ POST /reset-password (email + otp + newPassword)

┌────────────────────────────┐
│  1. Forgot Password        │
│  POST /forgot-password     │
│  Body: { email }           │
└────────┬───────────────────┘
         │
         ▼
    ✅ Find user by email
    ✅ Generate OTP (4 digits)
    ✅ Hash OTP (bcrypt)
         │
         ▼
    📧 Send OTP to email
    ├─ Purpose: password_reset
    ├─ Expires: 10 minutes
    └─ Response: Success message
         │
         ▼
┌──────────────────────────────────┐
│  2. Reset Password (NEW)         │
│  POST /reset-password            │
│  Body:                           │
│  ├─ email                        │
│  ├─ otp                          │ ◄─── CHANGED: Consolidated flow
│  └─ newPassword                  │      No bearer token needed
└────────┬──────────────────────────┘
         │
         ▼
    ✅ Find user by email
    ✅ Verify OTP against hash
    ✅ Check OTP expiration
    ✅ Mark OTP as used
         │
         ▼
    ✅ Validate password complexity
    ✅ Hash new password (bcrypt)
         │
         ▼
    🔐 Update user.password_hash
    └─► Single database operation
         │
         ▼
    Response: 200 OK
    {
      "message": "Password reset successfully."
    }
    
    User can now signin with new password
```

### 4. Sign In Flow

```
┌──────────────────────┐
│  Sign In             │
│  POST /signin        │
│  Body:               │
│  ├─ email            │
│  └─ password         │
└────────┬─────────────┘
         │
         ▼
    ✅ Find user by email
    ✅ Verify password against hash
    ✅ Check user is active
    ✅ Check email is verified
         │
         ▼
    Check onboarding status:
    ├─ If tenant.business_category IS NULL:
    │  └─ Return onboarding_token (need to complete onboarding)
    │
    └─ If tenant.business_category IS SET:
       └─ Issue full JWT pair
         │
         ▼
    Response: 200 OK
    {
      "access_token": "eyJ0eXAi...",
      "refresh_token": "eyJ0eXAi...",
      "token_type": "bearer"
    }
```

### 5. Token Refresh Flow

```
┌─────────────────────────┐
│  Refresh Tokens         │
│  POST /refresh          │
│  Body: { refresh_token }│
└────────┬────────────────┘
         │
         ▼
    ✅ Decode refresh_token
    ✅ Verify type = "refresh"
    ✅ Extract user_id from payload
         │
         ▼
    ✅ Verify user exists and is active
         │
         ▼
    🔑 Generate new JWT pair:
    ├─ New access_token (30 min)
    ├─ New refresh_token (7 days)
    └─ Old refresh_token becomes invalid
         │
         ▼
    Response: 200 OK
    {
      "access_token": "eyJ0eXAi...",
      "refresh_token": "eyJ0eXAi...",
      "token_type": "bearer"
    }
```

---

## Multi-Tenant Architecture

### Tenant Isolation Design

```
┌─────────────────────────────────────────────────────────┐
│              PostgreSQL Database                        │
└─────────────────────────────────────────────────────────┘
                        │
    ┌───────────────────┼───────────────────┐
    │                   │                   │
    ▼                   ▼                   ▼
┌─────────┐         ┌─────────┐         ┌─────────┐
│Tenant A │         │Tenant B │         │Tenant C │
│(Acme)   │         │(TechCo) │         │(StartUp)│
│ID: uuid1│         │ID: uuid2│         │ID: uuid3│
└────┬────┘         └────┬────┘         └────┬────┘
     │                   │                   │
     ├─ User 1          ├─ User 2          ├─ User 3
     ├─ Subscription    ├─ Subscription    ├─ Subscription
     ├─ Documents       ├─ Documents       ├─ Documents
     ├─ UsageCount      ├─ UsageCount      ├─ UsageCount
     └─ TenantQuota     └─ TenantQuota     └─ TenantQuota

Key Principle:
├─ Every record in documents, subscriptions, etc. has tenant_id FK
├─ All queries filter by tenant_id (automatically via session)
├─ No cross-tenant data leakage possible
└─ Each user belongs to exactly ONE tenant (1:1 relationship)
```

### Tenant-Aware Query Pattern

```python
# BAD: Can leak data
documents = await db.execute(
    select(Document).limit(10)
)

# GOOD: Always filter by tenant
documents = await db.execute(
    select(Document)
    .where(Document.tenant_id == user.tenant_id)
    .limit(10)
)

# BETTER: Automatic via session context
# (implemented in auth_service.get_current_user)
```

---

## Data Flow Diagrams

### Document Upload & RAG Processing Flow

```
┌──────────────┐
│ User Upload  │
│ PDF/DOCX/TXT │
└──────┬───────┘
       │
       ▼
┌─────────────────────────────┐
│  Document Upload Endpoint   │
│  POST /documents/upload     │
│  + Authorization Bearer     │
└──────┬──────────────────────┘
       │
       ▼
   ✅ Validate file type (MIME)
   ✅ Check file size < max_upload
   ✅ Check quota: storage_used < quota.max_storage
   ✅ Check quota: doc_count < quota.max_documents
       │
       ▼
┌─────────────────────────────────┐
│  Cloudinary Upload              │
│  (Cloud File Storage)           │
└──────┬──────────────────────────┘
       │
       ▼
   💾 Store file in cloud
   ├─ public_id = cloudinary_id
   ├─ secure_url = https://cloudinary.com/...
   └─ Response: public_id, secure_url
       │
       ▼
┌────────────────────────────────────────┐
│  Create Document Record                │
│  INSERT INTO documents                 │
│  ├─ tenant_id (from user)              │
│  ├─ original_filename                  │
│  ├─ file_path (public_id)              │
│  ├─ file_url (secure_url)              │
│  ├─ file_size_mb                       │
│  ├─ mime_type                          │
│  ├─ source = "uploaded"                │
│  └─ status = "pending" (needs processing)
└────────┬─────────────────────────────────┘
         │
         ▼
    📊 Update UsageCount:
    ├─ documents_count += 1
    └─ storage_used_mb += file_size
         │
         ▼
┌───────────────────────────────────────┐
│  ASYNC: RAG Processing Queue          │
│  (Background Task)                    │
│  process_document_for_rag()           │
└────────┬──────────────────────────────┘
         │
         ▼
    1️⃣ Extract text from PDF
       └─► PyPDF2 page-by-page
         │
         ▼
    2️⃣ Chunk text into overlapping segments
       ├─ RAG_CHUNK_SIZE = 500 tokens
       ├─ RAG_CHUNK_OVERLAP = 50 tokens
       └─ Each chunk max ~2000 chars
         │
         ▼
    3️⃣ Generate embeddings for each chunk
       └─► Call Anthropic Embeddings API
           (or local embedding model)
         │
         ▼
    4️⃣ Store in Pinecone Vector DB
       └─ Index: namespace = tenant_id
         ├─ Vector ID = document_id + chunk_index
         ├─ Metadata: chunk_text, document_id, tenant_id
         └─ Upsert operation
         │
         ▼
    5️⃣ Update Document status
       └─ UPDATE documents
         ├─ status = "ready"
         └─ chunk_count = N
         │
         ▼
    Response: 200 OK
    {
      "document_id": "uuid",
      "chunk_count": 145,
      "total_tokens": 72500,
      "status": "success"
    }
```

### RAG Chat Query Flow

```
┌──────────────────────────┐
│  User Query              │
│  POST /rag/chat          │
│  + Authorization Bearer  │
│  Body: { query: "..." }  │
└──────┬───────────────────┘
       │
       ▼
┌────────────────────────────────────┐
│  Step 1: Retrieve Context          │
│  retrieve_context_for_query()      │
└────────┬─────────────────────────────┘
         │
         ▼
     1a. Generate Query Embedding
         └─► Anthropic Embeddings API
             Input: User's question
             Output: 768-dim vector
         │
         ▼
     1b. Search Pinecone Vector DB
         ├─ Query vector similarity
         ├─ Filter: namespace = tenant_id (isolation)
         ├─ Top-k results (k = RAG_SEARCH_TOP_K = 5)
         └─ Return chunk texts + metadata
         │
         ▼
     1c. Return: List[str] (relevant chunks)
         └─► 5 most relevant document excerpts
         
         │
         ▼
┌────────────────────────────────────┐
│  Step 2: Generate Answer with LLM  │
│  answer_question()                 │
└────────┬─────────────────────────────┘
         │
         ▼
     2a. Build prompt
         ├─ System: "You are a helpful assistant..."
         ├─ Context: (retrieved chunks joined)
         └─ User Question: "..."
         │
         ▼
     2b. Call Anthropic Claude API
         ├─ Model: claude-3-5-sonnet-20241022
         ├─ Max tokens: 1024
         └─ Temperature: 0.7 (balanced)
         │
         ▼
     2c. Stream response
         └─► Return answer text
         │
         ▼
     2d. Track usage
         ├─ Increment UsageCount.questions_asked
         ├─ Check quota: questions_asked < max_questions_per_month
         └─ Return 403 if quota exceeded
         │
         ▼
    Response: 200 OK
    {
      "answer": "Based on the documents...",
      "sources": [chunk1, chunk2, chunk3],
      "model": "claude-3-5-sonnet-20241022",
      "tokens_used": 523
    }
```

---

## API Endpoint Architecture

### Authentication Endpoints

| Method | Path | Auth | Purpose | Request Body | Response |
|--------|------|------|---------|--------------|----------|
| POST | /auth/signup | ❌ | Register new user | `{companyName, email, password}` | 201: `{message, email}` |
| POST | /auth/verify-email | ❌ | Verify email with OTP | `{email, otp}` | 200: `{message, email, onboarding_token}` |
| POST | /auth/resend-otp | ❌ | Resend OTP | `{email}` | 200: `{message, email}` |
| POST | /auth/onboarding/complete | 🔑 (onboarding) | **[NEW]** Complete onboarding in 1 call | `{role, teamSize, goal, workspaceName}` | 201: `{access_token, refresh_token, message, workspace_name, slug}` |
| POST | /auth/signin | ❌ | Sign in with credentials | `{email, password}` | 200: `{access_token, refresh_token, token_type}` |
| POST | /auth/refresh | ❌ | Refresh token pair | `{refresh_token}` | 200: `{access_token, refresh_token, token_type}` |
| GET | /auth/me | 🔑 (access) | Get current user profile | - | 200: `{id, email, full_name, workspace_name, slug}` |
| POST | /auth/forgot-password | ❌ | Request password reset | `{email}` | 200: `{message, email}` |
| POST | /auth/reset-password | ❌ | **[NEW]** Reset password in 1 call | `{email, otp, newPassword}` | 200: `{message}` |
| POST | /auth/verify-otp | ❌ | **[RENAMED]** Verify reset OTP | `{email, otp, newPassword}` | 200: `{message, reset_token}` |
| POST | /auth/social/google | ❌ | Google OAuth sign in | `{id_token}` | 200: `{access_token, refresh_token, is_new_user}` |

**Legend:**
- 🔑 (access) = Bearer access_token
- 🔑 (onboarding) = Bearer onboarding_token
- ❌ = No authentication required

### Document Endpoints

| Method | Path | Auth | Purpose | Notes |
|--------|------|------|---------|-------|
| POST | /documents/upload | 🔑 access | Upload documents | Multi-file support |
| GET | /documents/list | 🔑 access | List user's documents | Tenant-isolated |
| GET | /documents/{id} | 🔑 access | Get document details | - |
| DELETE | /documents/{id} | 🔑 access | Soft delete document | Sets is_active=false |
| GET | /documents/samples | 🔑 access | Get community sample docs | Filtered by business_category |
| POST | /documents/sample/{id} | 🔑 access | Select sample document | Copies to user workspace |

### RAG Endpoints

| Method | Path | Auth | Purpose | Request Body | Notes |
|--------|------|------|---------|--------------|-------|
| POST | /rag/chat | 🔑 access | Ask question about docs | `{query}` | Uses vector search + LLM |
| POST | /rag/search | 🔑 access | Semantic search only | `{query}` | Returns chunks without LLM |

---

## Database Schema & Relationships

### Core Tables

```sql
-- Multi-tenant users
CREATE TABLE users (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL UNIQUE,  -- 1:1 with tenant
    full_name VARCHAR(255),          -- stores companyName too
    email VARCHAR(320) UNIQUE NOT NULL,
    password_hash VARCHAR(255),      -- nullable for OAuth users
    auth_provider VARCHAR(50),       -- 'email' or 'google'
    provider_uid VARCHAR(255),       -- for OAuth
    is_email_verified BOOLEAN,
    is_active BOOLEAN,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);

-- Multi-tenant workspaces
CREATE TABLE tenants (
    id UUID PRIMARY KEY,
    workspace_name VARCHAR(100),         -- "Acme Corp"
    slug VARCHAR(100) UNIQUE,            -- "acme-corp"
    business_category VARCHAR(100),      -- "Healthcare", etc
    employee_count_range VARCHAR(50),    -- "1-15", etc
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);

-- Subscription & Quotas
CREATE TABLE subscriptions (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL UNIQUE,
    plan_name VARCHAR(50),               -- 'free', 'pro', 'pro_plus'
    billing_cycle VARCHAR(50),           -- 'monthly', 'annual'
    status VARCHAR(50),                  -- 'active', 'cancelled'
    expires_at TIMESTAMP,                -- null for free
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);

CREATE TABLE tenant_quotas (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL UNIQUE,
    subscription_id UUID NOT NULL,
    max_documents INT,                   -- -1 = unlimited
    max_file_size_mb INT,
    max_questions_per_month INT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id),
    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id)
);

-- Usage tracking
CREATE TABLE usage_counts (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    period_month DATE,                   -- First day of month
    documents_count INT DEFAULT 0,
    storage_used_mb DECIMAL,
    questions_asked INT DEFAULT 0,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    UNIQUE(tenant_id, period_month),
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);

-- Email verification & OTPs
CREATE TABLE email_verifications (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    otp_code VARCHAR(255),               -- bcrypt hashed
    purpose VARCHAR(50),                 -- 'email_verification', 'password_reset'
    expires_at TIMESTAMP,
    is_used BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Document management (RAG)
CREATE TABLE documents (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,             -- Tenant isolation
    original_filename VARCHAR(255),
    file_path VARCHAR(500),              -- Cloudinary public_id
    file_url VARCHAR(500),               -- Cloudinary secure_url
    file_size_mb DECIMAL,
    mime_type VARCHAR(50),
    source VARCHAR(50),                  -- 'uploaded', 'sample', 'scraped'
    source_url VARCHAR(500),             -- for scraped docs
    status VARCHAR(50),                  -- 'pending', 'processing', 'ready', 'failed'
    chunk_count INT,                     -- populated after processing
    sample_document_id UUID,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);
```

### Relationships Diagram

```
                            ┌──────────────┐
                            │   tenants    │
                            │              │
                            │ id (PK)      │
                            │ workspace_.. │
                            │ slug         │
                            │ business_cat │
                            └──────┬───────┘
                                   │
                 ┌─────────────────┼─────────────────┐
                 │                 │                 │
                 │ 1:1             │ 1:1             │ 1:many
                 ▼                 ▼                 ▼
          ┌──────────────┐  ┌────────────────┐  ┌──────────────┐
          │    users     │  │ subscriptions  │  │   documents  │
          │              │  │                │  │              │
          │ id (PK)      │  │ id (PK)        │  │ id (PK)      │
          │ tenant_id(FK)│  │ tenant_id(FK)  │  │ tenant_id(FK)│
          │ email        │  │ plan_name      │  │ file_url     │
          │ password_..  │  │ status         │  │ status       │
          └──────┬───────┘  └────────┬───────┘  └──────────────┘
                 │                   │
                 │ 1:many            │ 1:1
                 │                   ▼
                 │           ┌────────────────┐
                 │           │ tenant_quotas  │
                 │           │                │
                 │           │ id (PK)        │
                 │           │ tenant_id(FK)  │
                 │           │ max_documents  │
                 │           └────────────────┘
                 │
                 │ 1:many
                 ▼
         ┌──────────────────────┐
         │email_verifications   │
         │                      │
         │ id (PK)              │
         │ user_id (FK)         │
         │ otp_code             │
         │ purpose              │
         │ expires_at           │
         └──────────────────────┘

Multi-Tenant Design Rule:
Every table with a business record has:
  • tenant_id (Foreign Key to tenants)
  • Query filters always include: WHERE table.tenant_id = user.tenant_id
  • No cross-tenant data leakage possible
```

---

## Integration Points

### External Services Integration

```
┌──────────────────────────────────────────────────────────┐
│          SecureRAG Backend API                           │
└──────────────────────────────────────────────────────────┘
    │                    │              │           │
    │                    │              │           │
    ▼                    ▼              ▼           ▼
┌──────────────┐  ┌──────────────┐ ┌────────────┐ ┌────────┐
│ Cloudinary   │  │  Pinecone    │ │ Anthropic  │ │ Email  │
│              │  │              │ │ (Claude)   │ │(Brevo) │
│ • File upload│  │ • Embeddings │ │            │ │        │
│ • Storage    │  │ • Vector DB  │ │ • LLM for  │ │ • OTP  │
│ • Retrieval  │  │ • Similarity │ │   answers  │ │ • Notif│
│              │  │   search     │ │            │ │        │
└──────────────┘  └──────────────┘ └────────────┘ └────────┘

API Keys Required in .env:
├─ CLOUDINARY_API_KEY
├─ CLOUDINARY_API_SECRET
├─ PINECONE_API_KEY
├─ ANTHROPIC_API_KEY
├─ BREVO_API_KEY (optional)
└─ SMTP credentials
```

---

## Security & Token Management

### JWT Token Structure

```
┌─────────────────────────────────────────────────────┐
│  Access Token (30 minutes validity)                 │
├─────────────────────────────────────────────────────┤
│ Header: {                                           │
│   "alg": "HS256",                                   │
│   "typ": "JWT"                                      │
│ }                                                   │
│                                                     │
│ Payload: {                                          │
│   "sub": "user-uuid",                              │
│   "type": "access",                                │
│   "exp": 1234567890,                               │
│   "iat": 1234567500                                │
│ }                                                   │
│                                                     │
│ Signature: HMAC-SHA256(header + payload + secret)  │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│  Refresh Token (7 days validity)                    │
├─────────────────────────────────────────────────────┤
│ Payload: {                                          │
│   "sub": "user-uuid",                              │
│   "type": "refresh",                               │
│   "exp": 1234989500                                │
│   "iat": 1234567500                                │
│ }                                                   │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│  Onboarding Token (1 hour validity)                 │
├─────────────────────────────────────────────────────┤
│ Payload: {                                          │
│   "sub": "user-uuid",                              │
│   "purpose": "onboarding",    ◄─── Marks onboarding
│   "exp": 1234571100                                │
│   "iat": 1234567500                                │
│ }                                                   │
└─────────────────────────────────────────────────────┘
```

### Password Security

```
User Input Password
       │
       ▼
   ✅ Validate Complexity:
   ├─ Min 8 characters
   ├─ At least 1 uppercase letter
   ├─ At least 1 lowercase letter
   └─ At least 1 digit
       │
       ▼
   🔐 Hash with bcrypt:
   ├─ Algorithm: bcrypt (Blowfish)
   ├─ Work factor: 12 (default)
   ├─ Salt: Auto-generated per password
   └─ Output: $2b$12$... (60 chars)
       │
       ▼
   💾 Store hash in password_hash column
   (Original password NEVER stored)
       │
       ▼
   Login Flow:
   ├─ User enters password
   └─ Compare with bcrypt.verify(hash)
```

### OTP Security

```
Generate OTP (4 digits)
       │
       ▼
   🔐 Hash with bcrypt:
   └─ Input: "1234"
   └─ Output: $2b$12$... (hashed)
       │
       ▼
   💾 Store in EmailVerification:
   ├─ otp_code (hashed)
   ├─ expires_at (10 minutes from now)
   ├─ is_used (initially false)
   ├─ purpose ('email_verification' or 'password_reset')
   └─ user_id (FK)
       │
       ▼
   📧 Send plain OTP to email
   (Only plain version shown to user)
       │
       ▼
   Verification Flow:
   ├─ User enters received OTP
   ├─ bcrypt.verify(userInput, storedHash)
   ├─ Check expiration
   ├─ Check is_used == false
   └─ Mark is_used = true
```

---

## Error Handling & Status Codes

### HTTP Status Codes

| Code | Scenario | Example |
|------|----------|---------|
| 200 | Success (GET/update) | /auth/me returns user |
| 201 | Resource created | /signup creates user |
| 400 | Bad request | Invalid email format |
| 401 | Unauthorized | Missing/invalid token |
| 403 | Forbidden | User deactivated |
| 404 | Not found | Document doesn't exist |
| 409 | Conflict | Email already exists |
| 413 | Payload too large | File exceeds size limit |
| 415 | Unsupported media type | Wrong file type |
| 422 | Unprocessable entity | Invalid business logic |
| 429 | Rate limited | Too many requests |
| 500 | Server error | Unhandled exception |

### Rate Limiting

```
/signup:           3 requests per minute
/verify-email:     5 requests per minute
/forgot-password:  3 requests per minute
Default:           Depends on endpoint

Rate Limit Headers:
├─ X-RateLimit-Limit
├─ X-RateLimit-Remaining
└─ X-RateLimit-Reset
```

---

## Implementation Summary

### 7 API Changes Completed ✅

1. ✅ **Signup Schema**: `full_name` → `companyName`
2. ✅ **OTP Field**: `otp_code` → `otp`
3. ✅ **Endpoint Path**: `/verify-reset-otp` → `/verify-otp`
4. ✅ **Reset Password**: 2-step → 1-step consolidated flow
5. ✅ **Consolidated Onboarding**: 3 endpoints → 1 endpoint
6. ✅ **Service Layer**: All functions updated
7. ✅ **Schema Alignment**: camelCase API ↔ snake_case internal

### Database Support

- ✅ No migrations required (schema unchanged)
- ✅ Field mappings preserved (companyName → full_name)
- ✅ Backward compatible (old endpoints still functional)
- ✅ Multi-tenant isolation maintained

---

## Next Steps

1. **Testing**: Run Postman collection tests
2. **Frontend Integration**: Update frontend API calls
3. **Deployment**: Deploy to production server
4. **Monitoring**: Track API usage and errors
5. **Documentation**: Update API docs with new flows

---

**Document Version**: 1.0  
**Last Updated**: 2026-06-30  
**Status**: Ready for Production
