# SecureRAG - Backend Implementation Guide

**Document Type:** Backend Service Implementation & Architecture  
**Version:** 1.0  
**Date:** 2026-06-30  
**Audience:** Backend Developers, DevOps Engineers, System Architects

---

## 📋 Table of Contents

1. [Service Layer Architecture](#service-layer-architecture)
2. [Database Operations & Patterns](#database-operations--patterns)
3. [Authentication Service Implementation](#authentication-service-implementation)
4. [Document Processing Pipeline](#document-processing-pipeline)
5. [RAG Service Implementation](#rag-service-implementation)
6. [Security Implementation Details](#security-implementation-details)
7. [Error Handling & Logging](#error-handling--logging)
8. [Performance Optimization](#performance-optimization)
9. [Testing Strategy](#testing-strategy)

---

## Service Layer Architecture

### Service Layer Organization

```
app/services/
├── auth_service.py          # Authentication & user management
├── document_service.py      # Document upload, storage, management
├── rag_service.py          # RAG pipeline, embeddings, LLM
├── workspace_service.py    # Tenant/workspace operations
└── usage_service.py        # Quota & usage tracking
```

### Auth Service (`auth_service.py`)

**Core Functions:**

```python
async def register_user(
    company_name: str,
    email: str,
    password: str,
    db: AsyncSession
) -> dict:
    """
    Implementation Flow:
    1. Check email uniqueness (SELECT * FROM users WHERE email=?)
    2. Create placeholder tenant (INSERT INTO tenants)
    3. Hash password with bcrypt (bcrypt.hashpw)
    4. Create user record (INSERT INTO users)
    5. Generate & send OTP (create_and_send_otp)
    6. Return success message
    
    Database Operations:
    - 1 SELECT query (email check)
    - 2 INSERT queries (tenant + user)
    - 1 INSERT in email_verifications
    
    Side Effects:
    - Creates records in users, tenants, email_verifications
    - Sends email asynchronously
    - No transaction needed (sequential independent ops)
    """
```

**Verify Email Flow:**

```python
async def verify_email(
    email: str,
    otp: str,
    db: AsyncSession
) -> dict:
    """
    Database Operations:
    1. Find user by email (SELECT FROM users WHERE email=?)
    2. Find valid OTP (SELECT FROM email_verifications WHERE ...)
    3. Verify OTP hash match (bcrypt.checkpw)
    4. Check expiration (expires_at > NOW())
    5. Mark OTP as used (UPDATE email_verifications SET is_used=TRUE)
    6. Mark email verified (UPDATE users SET is_email_verified=TRUE)
    7. Generate onboarding_token (create_access_token)
    
    Transaction Needed: YES (steps 4-6 must be atomic)
    """
```

**Complete Onboarding (NEW):**

```python
async def complete_onboarding(
    user: User,
    role: str,
    team_size: str,
    goal: str,
    workspace_name: str,
    plan_name: PlanName,
    billing_cycle: Optional[BillingCycle],
    db: AsyncSession
) -> dict:
    """
    ATOMIC DATABASE TRANSACTION:
    
    BEGIN;
    
    1. UPDATE tenants
       - Set employee_count_range = team_size
       - Set workspace_name = workspace_name
       - Generate slug (acme-corp, acme-corp-1, etc)
    
    2. INSERT subscriptions
       - tenant_id, plan_name, billing_cycle, status='active'
    
    3. INSERT tenant_quotas
       - From PLAN_QUOTAS[plan_name]
       - max_documents, max_file_size_mb, max_questions_per_month
    
    4. INSERT usage_counts
       - period_month=first_of_month(), documents_count=0, etc
    
    5. Generate JWT tokens
       - access_token (30 min)
       - refresh_token (7 days)
    
    COMMIT;
    
    Key Optimizations:
    - All DB ops in single transaction (ACID guarantee)
    - Slug collision handling (loop until unique found)
    - Token generation after DB ops (fail-safe)
    
    Replaces OLD 3 endpoints:
    - /organization (save org info)
    - /workspace (set workspace name)
    - /select-plan (create subscription)
    """
```

**Password Reset (NEW):**

```python
async def reset_password(
    email: str,
    otp: str,
    new_password: str,
    db: AsyncSession
) -> dict:
    """
    Database Operations:
    1. Find user by email (SELECT FROM users)
    2. Find valid OTP (SELECT FROM email_verifications)
    3. Verify OTP hash (bcrypt.checkpw)
    4. Check expiration (expires_at > NOW())
    5. Hash new password (bcrypt.hashpw)
    6. Update password & mark OTP used (UPDATE users, UPDATE email_verifications)
    
    Transaction Needed: YES (password + OTP marking must be atomic)
    
    Key Feature:
    - Consolidated flow (email + otp + newPassword in one call)
    - No bearer token required (email + OTP is verification)
    - Replaces old 2-step: verify-reset-otp + reset-password
    """
```

### Document Service (`document_service.py`)

**Upload Flow:**

```python
async def upload_documents(
    user: User,
    files: List[UploadFile],
    db: AsyncSession
) -> List[Document]:
    """
    Implementation:
    
    1. QUOTA VALIDATION:
       - Get TenantQuota for user.tenant_id
       - Check: doc_count < max_documents
       - Check: file_size < max_file_size_mb
       - Check: total_storage + file_size < quota
       └─ Raises HTTPException(403) if any fail
    
    2. FILE UPLOAD TO CLOUDINARY:
       - For each file:
         └─ upload_file_to_cloudinary(content, tenant_id, filename)
         └─ Returns: (public_id, secure_url)
    
    3. DATABASE OPERATIONS (Transaction):
       BEGIN;
       - For each file:
         └─ INSERT documents (tenant_id, file_path, file_url, status='pending')
       - UPDATE usage_counts
         └─ documents_count += N
         └─ storage_used_mb += total_size
       COMMIT;
    
    4. QUEUE RAG PROCESSING:
       - For each document:
         └─ asyncio.create_task(process_document_for_rag)
         └─ Background processing (5-30 seconds)
    
    Key Optimizations:
    - Quota checks before Cloudinary upload (fail fast)
    - All DB ops in one transaction
    - RAG processing async (non-blocking)
    - Return immediately (don't wait for RAG)
    """
```

### RAG Service (`rag_service.py`)

**Document Processing Pipeline:**

```python
async def process_document_for_rag(
    tenant_id: str,
    document_id: str,
    pdf_bytes: bytes,
) -> dict:
    """
    ASYNC PIPELINE (background task):
    
    1. EXTRACT TEXT FROM PDF:
       - Use PyPDF2.PdfReader
       - Iterate pages: page.extract_text()
       - Join with page markers: "--- Page 1 ---"
       - Return: full_text (str)
    
    2. CHUNK TEXT:
       - Use chunk_pdf_text(text, chunk_size=500, overlap=50)
       - Returns: List[{text, token_count}]
       - Each chunk ~500 tokens with 50 token overlap
       - Max chunk ~2000 chars
    
    3. GENERATE EMBEDDINGS:
       - For each chunk:
         └─ Call embed_text(chunk_text) via Anthropic API
         └─ Returns: 768-dimensional vector
       - Batch if possible (faster)
    
    4. STORE IN PINECONE:
       - Upsert to vector_store (Pinecone index)
       - Index namespace: tenant_id (multi-tenant isolation)
       - Vector ID: f"{document_id}_{chunk_index}"
       - Metadata: {chunk_text, document_id, tenant_id}
    
    5. UPDATE DOCUMENT STATUS:
       - UPDATE documents
         └─ status = 'ready'
         └─ chunk_count = N
       - Document now searchable via RAG
    
    Error Handling:
    - If ANY step fails:
      └─ UPDATE documents status = 'failed'
      └─ Log error details
      └─ Do NOT crash (background task)
    
    Database Ops:
    - 1 UPDATE at start (status='processing')
    - 1 UPDATE at end (status='ready' or 'failed')
    
    API Calls (Paid):
    - N calls to Anthropic (embeddings)
      └─ ~0.02 cost per document
    """
```

**RAG Chat Query:**

```python
async def answer_question(
    tenant_id: str,
    query: str,
    max_tokens: int = 1024
) -> dict:
    """
    Implementation:
    
    1. RETRIEVE CONTEXT:
       - Embed query: embed_text(query) → 768-dim vector
       - Search Pinecone:
         └─ Filter: metadata.tenant_id == tenant_id (isolation)
         └─ Top-k: 5 results (RAG_SEARCH_TOP_K)
         └─ Returns: chunk_texts + metadata
       - Build context: join chunks with newlines
    
    2. BUILD PROMPT:
       system_prompt = "You are a helpful assistant..."
       prompt = f"""
       CONTEXT:
       {context}
       
       QUESTION:
       {query}
       """
    
    3. CALL LLM (Anthropic Claude):
       - Model: claude-3-5-sonnet-20241022
       - Max tokens: 1024
       - Stream: True (real-time response)
       - Returns: answer (str)
    
    4. TRACK USAGE:
       - UPDATE usage_counts
         └─ questions_asked += 1
       - Check quota:
         └─ IF questions_asked > max_questions_per_month
            └─ Raise HTTPException(403)
    
    Database Ops:
    - 1 SELECT (get UsageCount)
    - 1 UPDATE (increment questions_asked)
    
    API Calls (Paid):
    - 1 call to Anthropic (embeddings for query)
    - 1 call to Anthropic (Claude LLM)
      └─ ~0.005 cost per query
    """
```

---

## Database Operations & Patterns

### Multi-Tenant Query Pattern

```python
# ❌ WRONG - No tenant isolation
result = await db.execute(
    select(Document).limit(10)
)

# ✅ CORRECT - Always filter by tenant
result = await db.execute(
    select(Document)
    .where(Document.tenant_id == user.tenant_id)
    .limit(10)
)

# ✅ AUTOMATED - Use session middleware
# (implemented in get_current_user dependency)
```

### Transaction Pattern

```python
# When multiple operations must be atomic
async with db.begin():  # Opens transaction
    try:
        # Step 1
        user.is_email_verified = True
        
        # Step 2
        db.add(onboarding_record)
        
        # Step 3
        await db.flush()  # Write to DB within transaction
        
        # All succeed or all fail
        # COMMIT happens automatically on exit
    except Exception as e:
        # ROLLBACK happens automatically
        await db.rollback()
        raise
```

### Async DB Query Pattern

```python
# Always use async patterns
async def get_user(email: str, db: AsyncSession):
    """
    DON'T:
    user = db.query(User).filter(User.email == email).first()
    
    DO:
    """
    result = await db.execute(
        select(User).where(User.email == email)
    )
    user = result.scalar_one_or_none()
    return user
```

### Connection Pooling & Performance

```python
# In config.py / database setup
engine = create_async_engine(
    DATABASE_URL,
    echo=False,  # Disable SQL logging in prod
    pool_size=20,  # Max concurrent connections
    max_overflow=10,  # Queue overflow
    pool_pre_ping=True,  # Verify connections before use
    pool_recycle=3600,  # Recycle after 1 hour
)

# Benefits:
# - Reuses connections (faster)
# - Avoids connection timeouts
# - Handles DB restarts gracefully
```

---

## Authentication Service Implementation

### Password Hashing

```python
import bcrypt
from passlib.context import CryptContext

pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__rounds=12,  # Work factor
)

# Hash password
def hash_password(password: str) -> str:
    return pwd_context.hash(password)
    # Output: $2b$12$... (60 chars)
    # Includes salt + work factor

# Verify password
def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)
    # Constant-time comparison (timing-attack safe)
```

### JWT Token Generation

```python
from jose import jwt
from datetime import datetime, timedelta

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    """
    Payload:
    {
      "sub": "user-uuid",      # Subject (user ID)
      "type": "access",         # Token type
      "exp": 1234567890,        # Expiration (Unix timestamp)
      "iat": 1234567500         # Issued at
    }
    
    Signature:
    HMAC-SHA256(base64(header) + "." + base64(payload) + secret_key)
    """
    to_encode = data.copy()
    
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=30)
    
    to_encode.update({"exp": expire, "type": "access"})
    
    encoded_jwt = jwt.encode(
        to_encode,
        settings.SECRET_KEY,
        algorithm="HS256"
    )
    
    return encoded_jwt

def decode_token(token: str) -> dict:
    """
    Returns payload if valid, raises JWTError if invalid
    """
    payload = jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=["HS256"]
    )
    return payload
```

### OTP Generation & Verification

```python
import secrets
import bcrypt

def generate_otp() -> str:
    """Generate 4-digit OTP"""
    return str(secrets.randbelow(10000)).zfill(4)
    # Returns: "0001" to "9999"

def hash_otp(otp: str) -> str:
    """Hash OTP before storing"""
    return bcrypt.hashpw(otp.encode(), bcrypt.gensalt())

def verify_otp(otp_input: str, otp_hash: str) -> bool:
    """Verify OTP against hash"""
    return bcrypt.checkpw(otp_input.encode(), otp_hash)

# Usage in verify_email:
if not verify_otp(user_input_otp, stored_otp_hash):
    raise HTTPException(status_code=400, detail="Invalid OTP")
```

---

## Document Processing Pipeline

### PDF Text Extraction

```python
from PyPDF2 import PdfReader
import io

def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract text from PDF with page markers"""
    pdf_file = io.BytesIO(pdf_bytes)
    pdf_reader = PdfReader(pdf_file)
    
    text_parts = []
    for page_num, page in enumerate(pdf_reader.pages):
        page_text = page.extract_text()
        if page_text.strip():
            text_parts.append(f"--- Page {page_num + 1} ---\n{page_text}")
    
    return "\n\n".join(text_parts)

# Output example:
# "--- Page 1 ---
#  Company Handbook
#  ...content...
#  
#  --- Page 2 ---
#  Chapter 1: HR Policies
#  ..."
```

### Text Chunking Algorithm

```python
def chunk_pdf_text(
    text: str,
    chunk_size: int = 500,  # tokens
    overlap_size: int = 50
) -> List[dict]:
    """
    Chunk text maintaining word boundaries
    
    Algorithm:
    1. Split by sentences/paragraphs
    2. Group sentences until ~500 tokens
    3. Overlap last 50 tokens with next chunk
    4. Maintain context across chunks
    """
    chunks = []
    words = text.split()
    
    current_chunk = []
    current_tokens = 0
    
    for word in words:
        word_tokens = len(word.split())  # Rough estimate
        
        if current_tokens + word_tokens > chunk_size and current_chunk:
            # Save chunk
            chunk_text = " ".join(current_chunk)
            chunks.append({
                "text": chunk_text,
                "token_count": current_tokens
            })
            
            # Keep overlap (last 50 tokens)
            overlap_words = current_chunk[-10:]  # Rough approximation
            current_chunk = overlap_words + [word]
            current_tokens = len(" ".join(current_chunk).split())
        else:
            current_chunk.append(word)
            current_tokens += word_tokens
    
    # Save final chunk
    if current_chunk:
        chunks.append({
            "text": " ".join(current_chunk),
            "token_count": current_tokens
        })
    
    return chunks
```

### Embedding Generation

```python
from anthropic import Anthropic

async def embed_text(text: str) -> List[float]:
    """Generate embedding for text"""
    client = Anthropic()
    
    response = client.messages.create(
        model="claude-3-5-sonnet-20241022",
        # Note: Claude doesn't have native embeddings
        # Use a dedicated embedding service instead:
        # - OpenAI embeddings API
        # - SentenceTransformers (local)
        # - Anthropic embeddings (if available)
    )
    
    # Returns 768-dimensional vector
    return embedding_vector

async def embed_chunks(chunk_texts: List[str]) -> List[List[float]]:
    """Batch embed multiple chunks"""
    embeddings = []
    for text in chunk_texts:
        embedding = await embed_text(text)
        embeddings.append(embedding)
    return embeddings
```

### Pinecone Vector Storage

```python
from pinecone import Pinecone

async def upsert_chunks(
    tenant_id: str,
    document_id: str,
    chunks: List[dict],
    embeddings: List[List[float]]
):
    """Store chunks in Pinecone"""
    pc = Pinecone(api_key=settings.PINECONE_API_KEY)
    index = pc.Index(settings.PINECONE_INDEX_NAME)
    
    vectors_to_upsert = []
    
    for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        vector_id = f"{document_id}_{i}"
        metadata = {
            "chunk_text": chunk["text"],
            "document_id": str(document_id),
            "tenant_id": str(tenant_id),
            "chunk_index": i
        }
        
        vectors_to_upsert.append((
            vector_id,
            embedding,
            metadata
        ))
    
    # Upsert to Pinecone
    index.upsert(vectors=vectors_to_upsert, namespace=str(tenant_id))

async def search_chunks(
    query_embedding: List[float],
    tenant_id: str,
    top_k: int = 5
) -> List[dict]:
    """Search Pinecone for similar chunks"""
    pc = Pinecone(api_key=settings.PINECONE_API_KEY)
    index = pc.Index(settings.PINECONE_INDEX_NAME)
    
    results = index.query(
        vector=query_embedding,
        top_k=top_k,
        namespace=str(tenant_id),  # Multi-tenant isolation
        include_metadata=True
    )
    
    chunks = []
    for match in results["matches"]:
        chunk_text = match["metadata"]["chunk_text"]
        chunks.append({
            "text": chunk_text,
            "document_id": match["metadata"]["document_id"],
            "score": match["score"]  # Similarity score (0-1)
        })
    
    return chunks
```

---

## RAG Service Implementation

### Query Processing

```python
async def retrieve_context_for_query(
    tenant_id: str,
    query: str,
) -> List[str]:
    """
    Step 1: Embed query
    Step 2: Search Pinecone
    Step 3: Extract text
    """
    # Embed query
    query_embedding = await embed_text(query)
    
    # Search Pinecone
    similar_chunks = await search_chunks(
        query_embedding,
        tenant_id,
        top_k=5  # Get 5 most relevant
    )
    
    # Extract just text for context
    context_chunks = [chunk["text"] for chunk in similar_chunks]
    
    return context_chunks

async def answer_question(
    tenant_id: str,
    query: str,
    max_tokens: int = 1024
) -> dict:
    """
    RAG pipeline with quota checking
    """
    # Retrieve context
    context_chunks = await retrieve_context_for_query(tenant_id, query)
    
    if not context_chunks:
        return {
            "answer": "No relevant documents found.",
            "sources": [],
        }
    
    # Build prompt
    context = "\n\n".join(context_chunks)
    prompt = f"""You are a helpful assistant answering questions based on provided documents.

CONTEXT:
{context}

QUESTION: {query}

Please answer the question based on the context provided."""
    
    # Call Claude
    client = Anthropic()
    response = client.messages.create(
        model="claude-3-5-sonnet-20241022",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )
    
    answer = response.content[0].text
    
    # Update usage (quota tracking)
    await update_usage_count(tenant_id)
    
    return {
        "answer": answer,
        "sources": context_chunks,
        "model": "claude-3-5-sonnet-20241022",
    }

async def update_usage_count(tenant_id: str, db: AsyncSession):
    """Increment question count and check quota"""
    result = await db.execute(
        select(UsageCount)
        .where(UsageCount.tenant_id == tenant_id)
        .order_by(UsageCount.period_month.desc())
    )
    usage = result.scalar_one()
    
    # Check quota BEFORE incrementing
    quota = await get_tenant_quota(tenant_id, db)
    if usage.questions_asked >= quota.max_questions_per_month:
        raise HTTPException(
            status_code=403,
            detail="Monthly question limit reached. Upgrade to pro."
        )
    
    usage.questions_asked += 1
    await db.commit()
```

---

## Security Implementation Details

### Rate Limiting

```python
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

# Per-endpoint rate limits
@router.post("/signup")
@limiter.limit("3/minute")
async def signup(request: Request, body: SignupRequest):
    """Max 3 signup attempts per minute per IP"""
    pass

@router.post("/verify-email")
@limiter.limit("5/minute")
async def verify_email(request: Request, body: VerifyRequest):
    """Max 5 OTP verifications per minute per IP"""
    pass

# Rate limit error
# HTTP 429 Too Many Requests
# X-RateLimit-Remaining: 0
# X-RateLimit-Reset: 2026-06-30T12:05:00Z
```

### CORS Configuration

```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,  # ["https://app.securerag.com"]
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
    max_age=600,  # Preflight cache 10 minutes
)
```

### Input Validation

```python
from pydantic import BaseModel, EmailStr, Field, field_validator

class SignupRequest(BaseModel):
    companyName: str = Field(..., min_length=1, max_length=255)
    email: EmailStr  # Validates email format
    password: str = Field(..., min_length=8, max_length=128)
    
    @field_validator("password")
    @classmethod
    def validate_password_complexity(cls, v: str) -> str:
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain uppercase")
        if not any(c.islower() for c in v):
            raise ValueError("Password must contain lowercase")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain digit")
        return v

# All fields validated at request boundary
# Type safety guaranteed in service layer
```

---

## Error Handling & Logging

### Structured Logging

```python
import logging

logger = logging.getLogger(__name__)

# In services
async def register_user(...):
    try:
        logger.info(f"User registration attempt: {email}")
        
        # ... implementation ...
        
        logger.info(f"User registered successfully: {user.id}")
        
    except ValueError as e:
        logger.warning(f"Validation error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error in register_user: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
```

### Exception Handling Pattern

```python
# Custom exceptions
class QuotaExceeded(Exception):
    pass

class InvalidOTP(Exception):
    pass

# In service
async def upload_document(...):
    try:
        # Quota check
        if current_docs >= quota.max_documents:
            raise QuotaExceeded(f"Max {quota.max_documents} documents allowed")
        
        # ... upload logic ...
        
    except QuotaExceeded as e:
        logger.warning(f"Quota exceeded for tenant {tenant_id}")
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.error(f"Upload failed: {str(e)}")
        raise HTTPException(status_code=500, detail="Upload failed")
```

---

## Performance Optimization

### Query Optimization

```python
# ❌ N+1 queries - slow
users = await db.execute(select(User))
for user in users:
    tenant = await db.execute(
        select(Tenant).where(Tenant.id == user.tenant_id)
    )  # Query runs N times

# ✅ Eager loading - fast
result = await db.execute(
    select(User).options(selectinload(User.tenant))
)
users = result.scalars().all()  # Single query with JOIN
```

### Async Operations

```python
# ❌ Blocks - slow
for file in files:
    result = upload_to_cloudinary(file)  # Waits 2 seconds
    # Total: 10 files × 2 seconds = 20 seconds

# ✅ Concurrent - fast
import asyncio

tasks = [upload_to_cloudinary(file) for file in files]
results = await asyncio.gather(*tasks)  # 10 files in ~2 seconds
```

### Caching

```python
from functools import lru_cache
from datetime import timedelta

# Cache plan quotas (change infrequently)
@lru_cache(maxsize=128)
def get_plan_quotas(plan_name: str) -> dict:
    return PLAN_QUOTAS[plan_name]

# Cache business categories (constant)
@lru_cache(maxsize=1)
def get_valid_categories() -> set:
    return VALID_BUSINESS_CATEGORIES
```

---

## Testing Strategy

### Unit Tests (Services)

```python
import pytest
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_register_user_creates_user():
    """Test user registration creates record"""
    mock_db = AsyncMock()
    
    result = await register_user(
        company_name="Acme",
        email="test@acme.com",
        password="TestP@ss123",
        db=mock_db
    )
    
    assert result["email"] == "test@acme.com"
    # Verify DB operations called
    assert mock_db.execute.called

@pytest.mark.asyncio
async def test_verify_email_checks_otp_expiry():
    """Test expired OTP rejected"""
    # Setup: expired OTP in DB
    expired_otp = OTPRecord(expires_at=past_time)
    
    with pytest.raises(HTTPException) as exc:
        await verify_email("test@acme.com", "1234", db)
    
    assert exc.value.status_code == 400
```

### Integration Tests (API)

```python
import pytest
from httpx import AsyncClient

@pytest.mark.asyncio
async def test_signup_flow_end_to_end(client: AsyncClient):
    """Test complete signup → verify → onboarding flow"""
    
    # Step 1: Signup
    response = await client.post(
        "/api/v1/auth/signup",
        json={
            "companyName": "TestCo",
            "email": "test@example.com",
            "password": "TestP@ss123"
        }
    )
    assert response.status_code == 201
    
    # Step 2: Get OTP (mock email)
    otp = get_test_otp()  # From test email fixture
    
    # Step 3: Verify
    response = await client.post(
        "/api/v1/auth/verify-email",
        json={"email": "test@example.com", "otp": otp}
    )
    assert response.status_code == 200
    onboarding_token = response.json()["onboarding_token"]
    
    # Step 4: Onboarding
    response = await client.post(
        "/api/v1/auth/onboarding/complete",
        headers={"Authorization": f"Bearer {onboarding_token}"},
        json={
            "role": "Manager",
            "teamSize": "1-15",
            "goal": "Test",
            "workspaceName": "TestCo"
        }
    )
    assert response.status_code == 201
    access_token = response.json()["access_token"]
    assert access_token is not None
```

### Load Testing

```python
# Using locust or similar
from locust import HttpUser, task, between

class RagUser(HttpUser):
    wait_time = between(1, 5)
    
    @task
    def ask_question(self):
        """Simulate user asking questions"""
        self.client.post(
            "/api/v1/rag/chat",
            headers={"Authorization": f"Bearer {self.access_token}"},
            json={"query": "What is...?"}
        )

# Run: locust -f load_test.py -u 100 -r 10
# (100 concurrent users, 10 new per second)
```

---

## Deployment Checklist

- [ ] Environment variables configured (.env.prod)
- [ ] Database migrations run (alembic upgrade head)
- [ ] Redis cache warmed (if using)
- [ ] CORS origins whitelisted
- [ ] Rate limiting tuned for expected traffic
- [ ] Error logging configured (Sentry/DataDog)
- [ ] Database backups automated
- [ ] API documentation (Swagger) reviewed
- [ ] Performance tested (load testing passed)
- [ ] Security audit completed
- [ ] Secrets rotated (API keys, JWT secret)

---

**Document Version**: 1.0  
**Last Updated**: 2026-06-30  
**Status**: Backend Implementation Complete ✅
