# RAG Architecture & Components

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      User Application                            │
│                    (Web/Mobile Client)                           │
└────────────────────────────┬────────────────────────────────────┘
                             │
                    ┌────────▼────────┐
                    │  FastAPI Server │
                    │ SecureRAG++ v1  │
                    └────────┬────────┘
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
    ┌───▼──────┐        ┌───▼──────┐        ┌───▼──────┐
    │ Auth API │        │ Docs API │        │ RAG API  │
    │          │        │          │        │          │
    │ /auth/*  │        │ /docs/*  │        │ /rag/*   │
    └──────────┘        └────┬─────┘        └────┬─────┘
                             │                    │
                   ┌─────────▼──────┐   ┌────────▼───────┐
                   │  PostgreSQL    │   │ Pinecone Index │
                   │  (Documents)   │   │  (Embeddings)  │
                   └────────────────┘   └────────┬───────┘
                                                 │
                          ┌──────────────────────┘
                          │
        ┌─────────────────┴────────────────────┐
        │                                      │
    ┌───▼──────────────────┐      ┌──────────▼────────┐
    │  Chunking Pipeline   │      │  LLM (Anthropic)  │
    │                      │      │                   │
    │ PDF Extract → Chunk  │      │  Claude 3.5       │
    │ → Embed → Pinecone   │      │  (Answer Gen)     │
    └──────────────────────┘      └───────────────────┘
```

## Component Overview

### 1. API Layer (`app/api/v1/endpoints/rag.py`)

**Role:** HTTP request/response handling

**Endpoints:**
- `POST /rag/query` - Q&A endpoint
- `POST /rag/health` - Health check
- `POST /rag/process-document/{id}` - Document indexing

**Responsibilities:**
- Parse and validate requests
- Extract authentication token
- Apply rate limiting
- Call service layer
- Return formatted responses

**Key Features:**
- Rate limiting via slowapi
- JWT authentication
- Error handling and logging
- OpenAPI documentation

### 2. RAG Service (`app/services/rag_service.py`)

**Role:** Business logic and orchestration

**Key Functions:**

#### process_document_for_rag()
```python
async def process_document_for_rag(
    tenant_id: str,
    document_id: str,
    pdf_bytes: bytes
) -> dict
```
Orchestrates full indexing pipeline:
1. Extract PDF text
2. Chunk with overlap
3. Generate embeddings
4. Store in Pinecone

#### retrieve_context_for_query()
```python
async def retrieve_context_for_query(
    tenant_id: str,
    query: str
) -> List[str]
```
Retrieves relevant document chunks:
1. Embed query
2. Search Pinecone (filtered by tenant)
3. Extract text from results

#### answer_question()
```python
async def answer_question(
    tenant_id: str,
    query: str,
    max_tokens: int = 1024
) -> dict
```
Generates LLM answer:
1. Retrieve context chunks
2. Build prompt with context
3. Call Claude API
4. Return answer + sources

### 3. Core Modules

#### Chunking (`app/core/chunking.py`)

**Purpose:** Break documents into optimal-sized pieces

**Key Functions:**

```python
def count_tokens(text: str) -> int
    # Count tokens using tiktoken (Claude-compatible)
    
def recursive_chunk(text, chunk_size=500, overlap_size=50) -> List[str]
    # Recursively split by tokens with overlap
    
def chunk_pdf_text(text, chunk_size=500, overlap_size=50) -> List[dict]
    # Chunk and add metadata (token count, sequence number)
```

**Why Recursive with Overlap?**
- Recursive split on semantically meaningful boundaries
- Token-based sizing for LLM cost consistency
- Overlap prevents context loss at chunk boundaries

**Example:**
```
Input: "In the beginning, Alice sat by the river..."
       (2500 tokens total)

Chunks (500 tokens, 50 overlap):
- Chunk 0 (500 tokens): "In the beginning, Alice sat..."
- Chunk 1 (500 tokens, 50 shared): "...while her sister read..."
- Chunk 2 (500 tokens, 50 shared): "...was getting very tired..."
- ...
Total: ~5 chunks for 2500 token document
```

#### Embeddings (`app/core/embeddings.py`)

**Purpose:** Convert text to semantic vectors

**Key Functions:**

```python
async def embed_text(text: str) -> List[float]
    # Generate embedding for single text
    
async def embed_chunks(texts: List[str]) -> List[List[float]]
    # Batch embed multiple texts
```

**Current Implementation (Development):**
- Simple hash-based embedding
- 1536-dimensional vectors
- Deterministic (same input = same output)
- No API calls needed

**Production Options:**

```python
# Option 1: OpenAI (Recommended)
# Best quality, $0.02 per 1M tokens
from openai import OpenAI
client = OpenAI()

async def embed_text_openai(text: str):
    response = client.embeddings.create(
        model="text-embedding-3-small",
        input=text
    )
    return response.data[0].embedding

# Option 2: Hugging Face (Local)
# Fast, free, no API key needed
from sentence_transformers import SentenceTransformer

model = SentenceTransformer('all-MiniLM-L6-v2')

async def embed_text_hf(text: str):
    embedding = model.encode(text)
    return embedding.tolist()

# Option 3: Cohere
from cohere import Client

client = Client(api_key="...")

async def embed_text_cohere(text: str):
    result = client.embed(texts=[text])
    return result.embeddings[0]
```

#### Vector Store (`app/core/vector_store.py`)

**Purpose:** Manage document vectors in Pinecone

**Architecture:**

```
Vector ID: {tenant_id}#{document_id}#{chunk_id}
           ├─ tenant_id: Multi-tenancy isolation
           ├─ document_id: Document reference
           └─ chunk_id: Chunk identifier

Metadata:
  {
    "tenant_id": "acme-corp",
    "document_id": "doc-123",
    "chunk_id": "chunk-0",
    "sequence": 0,
    "text": "Preview of chunk content...",
    "token_count": 487
  }

Vector: [0.142, 0.856, -0.234, ..., 0.521]
        1536 dimensions
```

**Key Functions:**

```python
async def upsert_chunks(
    tenant_id: str,
    document_id: str,
    chunks: List[Dict],
    embeddings: List[List[float]]
) -> None
    # Batch upsert vectors (100 at a time)
    # Metadata includes tenant_id for filtering
    
async def search_chunks(
    query_embedding: List[float],
    tenant_id: str,
    top_k: int = 5
) -> List[Dict]
    # Vector similarity search with tenant filter
    # Returns: score, text preview, metadata
    
async def delete_document_chunks(
    tenant_id: str,
    document_id: str
) -> None
    # Remove all chunks for a document
    
async def clear_tenant_data(tenant_id: str) -> None
    # GDPR-compliant tenant cleanup
```

**Multi-Tenancy Isolation:**

All Pinecone queries use filters:
```python
filter={
    "tenant_id": {"$eq": tenant_id}
}
```

This ensures:
- Tenant A cannot see Tenant B's vectors
- Efficient per-tenant namespace
- GDPR compliance (delete entire tenant)

## Data Flow

### Document Indexing Flow

```
User uploads PDF
       │
       ▼
┌──────────────────┐
│ PDF received     │
│ (encrypted)      │
└────────┬─────────┘
         │
         ▼
┌──────────────────────────┐
│ Extract text from PDF    │ ← PyPDF2
│ "Chapter 1: ..."         │
└────────┬─────────────────┘
         │
         ▼
┌─────────────────────────────────┐
│ Chunk into 500-token pieces     │ ← tiktoken
│ Overlap: 50 tokens              │
│ Result: [chunk1, chunk2, ...]   │
└────────┬────────────────────────┘
         │
         ▼
┌──────────────────────────────────┐
│ Generate embeddings              │
│ For each chunk: embed(text)      │
│ Result: [[0.1, 0.2, ...], ...]   │
└────────┬───────────────────────┘
         │
         ▼
┌─────────────────────────────────────┐
│ Store in Pinecone                   │
│ ID: tenant#{doc_id}#{chunk_id}      │
│ Vector: [1536 dims]                 │
│ Metadata: {tenant, doc, chunk, ...} │
└────────┬────────────────────────────┘
         │
         ▼
Document indexed ✓
```

### Query/Answer Flow

```
User asks: "What is the topic?"
           │
           ▼
┌───────────────────┐
│ Embed query       │ ← Same model as document chunks
│ Query vector: [..] │
└────────┬──────────┘
         │
         ▼
┌──────────────────────────────┐
│ Search Pinecone              │
│ Filter: tenant_id = user's   │
│ Find: top 5 similar vectors  │
└────────┬─────────────────────┘
         │
         ▼
┌───────────────────────────┐
│ Retrieve chunks           │
│ [chunk1, chunk2, chunk3,  │
│  chunk4, chunk5]          │
└────────┬──────────────────┘
         │
         ▼
┌────────────────────────┐
│ Build context         │
│ context = join(chunks)│
└────────┬───────────────┘
         │
         ▼
┌────────────────────────────────────┐
│ Call Claude                        │
│ Input: context + query             │
│ Model: claude-3-5-sonnet-20241022  │
└────────┬───────────────────────────┘
         │
         ▼
┌────────────────────┐
│ Answer generated   │
│ "The topic is..."  │
└────────┬───────────┘
         │
         ▼
Return to user:
{
  "answer": "The topic is...",
  "sources": [chunk1, chunk2, ...],
  "model": "claude-3-5-sonnet-20241022"
}
```

## Authentication & Authorization

### Multi-Tenancy Model

**Tenant Isolation:**
```
User (email: alice@acme.com)
  ├─ Tenant: acme-corp
  │   ├─ Documents (PostgreSQL)
  │   │   └─ Filtered by tenant_id
  │   └─ Vectors (Pinecone)
  │       └─ Filtered by tenant_id in metadata
  └─ Access Token (JWT)
      └─ Contains: user_id, tenant_id
```

**Authorization Flow:**
```
1. Validate JWT token
2. Extract tenant_id from token
3. All queries filtered by this tenant_id
4. User can only see their own data
```

### Rate Limiting

**Implementation:** slowapi (token bucket algorithm)

**Limits:**
- Query endpoint: 20/minute per IP
- Health endpoint: 10/minute per IP

**Rate Limit Headers:**
```
X-RateLimit-Limit: 20
X-RateLimit-Remaining: 15
X-RateLimit-Reset: 1234567890
```

## Performance Optimization

### Async/Await Architecture

```python
async def query_documents(query: str):
    # Non-blocking operations throughout
    embedding = await embed_text(query)        # I/O wait
    results = await search_chunks(embedding)   # I/O wait
    answer = await claude.create(...)          # I/O wait
    # Total: ~2 seconds vs ~6 seconds if synchronous
```

### Batch Operations

**Embedding:**
```python
embeddings = await embed_chunks(texts)  # Process all at once
```

**Vector Upsert:**
```python
for i in range(0, len(vectors), 100):
    batch = vectors[i:i+100]
    await pinecone.upsert(batch)  # 100 vectors per request
```

### Lazy Loading

**API Clients:**
```python
# Don't initialize on import
_pinecone = None
_anthropic = None

def _get_pinecone():
    global _pinecone
    if _pinecone is None:
        _pinecone = Pinecone(api_key=...)
    return _pinecone
```

**Benefits:**
- No startup failures if API key missing
- Delayed initialization until first use
- Allows dev/test without all credentials

## Security Considerations

### Data Security

1. **Multi-Tenancy:** Pinecone metadata filters prevent cross-tenant leakage
2. **Encryption at Rest:** Files encrypted before storage (via FILE_ENCRYPTION_KEY)
3. **Encryption in Transit:** HTTPS/TLS for all API calls
4. **JWT Tokens:** Time-limited, cryptographically signed

### API Security

1. **Authentication:** All endpoints require valid JWT
2. **Rate Limiting:** Prevents abuse and DDoS
3. **Input Validation:** Query minimum 3 characters
4. **Error Messages:** No sensitive information leaked

### Infrastructure Security

1. **Environment Variables:** API keys in .env, not in code
2. **No Logging:** Query text not logged (privacy)
3. **CORS:** Configured to allow specific origins only
4. **HTTPS:** Required in production

## Extension Points

### Custom Embedding Models

Replace `_simple_embedding()` in `app/core/embeddings.py`:

```python
async def embed_text(text: str) -> List[float]:
    # Replace this with your custom implementation
    result = await your_embedding_api.embed(text)
    return result
```

### Custom LLM for Answers

Modify `answer_question()` in `app/services/rag_service.py`:

```python
async def answer_question(...):
    ...
    # Replace Anthropic call
    response = await your_llm.generate(prompt)
    answer = response.text
    ...
```

### Additional Metadata

Store more in Pinecone metadata:

```python
metadata = {
    "tenant_id": tenant_id,
    "document_id": document_id,
    "chunk_id": chunk_id,
    "sequence": chunk["sequence"],
    "text": chunk["text"][:200],
    "token_count": chunk["token_count"],
    
    # Add custom fields:
    "page_number": 5,
    "source_url": "...",
    "upload_date": "2024-06-26",
}
```

## Monitoring & Observability

### Metrics to Track

1. **Latency:**
   - Embedding generation time
   - Vector search time
   - Claude response time

2. **Throughput:**
   - Queries per minute
   - Chunks indexed per hour
   - Batch processing speed

3. **Quality:**
   - Answer relevance (user feedback)
   - Source chunk relevance scores
   - Document coverage

4. **Errors:**
   - Failed embeddings
   - Search failures
   - LLM API timeouts

### Logging

```python
logger.info(f"Extracting text from PDF for document {document_id}")
logger.info(f"Chunking document into {len(chunks)} pieces")
logger.info(f"Storing {len(vectors)} vectors in Pinecone")
logger.error(f"Failed to process document: {str(e)}")
```

### Health Monitoring

Use `/api/v1/rag/health` endpoint to:
- Monitor Pinecone connectivity
- Check index capacity
- Verify configuration

## Limitations & Future Improvements

### Current Limitations

1. Simple embedding model (development only)
2. No conversation history/context
3. No reranking of retrieved chunks
4. No citation tracking with page numbers
5. Document indexing not fully integrated

### Future Improvements

1. **Production Embeddings:** OpenAI/Hugging Face
2. **Conversation History:** Multi-turn RAG with context
3. **Reranking:** Improve top-K before LLM
4. **Advanced Citations:** Page numbers, highlights
5. **Hybrid Search:** Keyword + semantic
6. **Caching:** Cache embeddings and answers
7. **Analytics:** Track query patterns, answer quality

---

**Last Updated:** 2026-06-26
**Version:** 1.0.0
