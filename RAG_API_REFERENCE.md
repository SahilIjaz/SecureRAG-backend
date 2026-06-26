# RAG API Reference

## Base URL
```
http://localhost:8000/api/v1/rag
```

## Authentication
All RAG endpoints require JWT authentication via Bearer token:

```
Authorization: Bearer {access_token}
```

Get token from `/api/v1/auth/signup` or `/api/v1/auth/signin`.

---

## POST /rag/query

Ask a question about indexed documents and get an LLM-powered answer grounded in the content.

### Request

```http
POST /api/v1/rag/query
Content-Type: application/json
Authorization: Bearer {access_token}

{
  "query": "What is the main topic?"
}
```

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | Yes | The question to ask (minimum 3 characters) |

### Response (200 OK)

```json
{
  "answer": "Based on the documents, the main topic is... [Claude's answer]",
  "sources": [
    "Retrieved chunk 1 from document...",
    "Retrieved chunk 2 from document...",
    "Retrieved chunk 3 from document..."
  ],
  "model": "claude-3-5-sonnet-20241022"
}
```

### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `answer` | string | LLM-generated answer grounded in document context |
| `sources` | array[string] | Retrieved document chunks used to generate answer |
| `model` | string | Claude model used for answer generation |

### Error Responses

**400 Bad Request** - Invalid or missing query
```json
{
  "detail": "Query must be at least 3 characters"
}
```

**401 Unauthorized** - Missing or invalid token
```json
{
  "detail": "Not authenticated"
}
```

**429 Too Many Requests** - Rate limit exceeded (20/minute)
```json
{
  "detail": "Rate limit exceeded. Please try again later."
}
```

**500 Internal Server Error** - Processing failed
```json
{
  "detail": "Failed to answer question: [error details]"
}
```

### Rate Limit
- **Limit:** 20 requests per minute per IP
- **Headers:**
  ```
  RateLimit-Limit: 20
  RateLimit-Remaining: 15
  RateLimit-Reset: 1234567890
  ```

### Examples

#### Example 1: Simple Topic Query
```bash
curl -X POST http://localhost:8000/api/v1/rag/query \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIs..." \
  -d '{"query": "What is the document about?"}'
```

#### Example 2: Specific Information
```bash
curl -X POST http://localhost:8000/api/v1/rag/query \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIs..." \
  -d '{"query": "How is security implemented?"}'
```

#### Example 3: With JQ for Pretty Output
```bash
curl -X POST http://localhost:8000/api/v1/rag/query \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer {token}" \
  -d '{"query": "Summarize the key findings"}' | jq .
```

---

## POST /rag/health

Check the health status of the vector store and RAG system.

### Request

```http
POST /api/v1/rag/health
Authorization: Bearer {access_token}
```

### Response (200 OK)

```json
{
  "status": "healthy",
  "vector_store": "pinecone",
  "dimension": 1536,
  "index_fullness": 0.23
}
```

### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | "healthy" if vector store is accessible |
| `vector_store` | string | "pinecone" (vector database service) |
| `dimension` | integer | Embedding dimension (1536 for current model) |
| `index_fullness` | float | 0.0-1.0, percentage of index capacity used |

### Error Responses

**503 Service Unavailable** - Vector store not accessible
```json
{
  "detail": "RAG system unavailable: Connection to Pinecone failed"
}
```

### Rate Limit
- **Limit:** 10 requests per minute per IP

### Examples

```bash
# Basic health check
curl -X POST http://localhost:8000/api/v1/rag/health \
  -H "Authorization: Bearer {token}"

# With pretty output
curl -X POST http://localhost:8000/api/v1/rag/health \
  -H "Authorization: Bearer {token}" | jq .

# Check for healthy status
curl -X POST http://localhost:8000/api/v1/rag/health \
  -H "Authorization: Bearer {token}" | jq '.status'
```

---

## POST /rag/process-document/{document_id}

Index a document for RAG retrieval (placeholder endpoint).

### Request

```http
POST /api/v1/rag/process-document/{document_id}
Authorization: Bearer {access_token}
```

### Path Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `document_id` | UUID | Document ID to index |

### Response (202 Accepted)

```json
{
  "document_id": "550e8400-e29b-41d4-a716-446655440000",
  "chunk_count": 45,
  "total_tokens": 22500,
  "status": "success"
}
```

### Current Status

⚠️ **Placeholder:** This endpoint requires integration with Cloudinary file storage.

To fully implement:
1. Download encrypted file from Cloudinary
2. Decrypt using FILE_ENCRYPTION_KEY
3. Extract PDF text
4. Chunk and embed
5. Store in Pinecone

### Rate Limit
- **Limit:** 5 requests per minute per IP

---

## Data Structures

### QueryRequest

Request body for `/query` endpoint:
```json
{
  "query": "string (required, min 3 chars)"
}
```

### QueryResponse

Response from `/query` endpoint:
```json
{
  "answer": "string",
  "sources": ["string", "string", ...],
  "model": "claude-3-5-sonnet-20241022"
}
```

### HealthResponse

Response from `/health` endpoint:
```json
{
  "status": "healthy",
  "vector_store": "pinecone",
  "dimension": 1536,
  "index_fullness": 0.23
}
```

### DocumentProcessResponse

Response from `/process-document/{id}` endpoint:
```json
{
  "document_id": "uuid",
  "chunk_count": 45,
  "total_tokens": 22500,
  "status": "success"
}
```

---

## Authentication Examples

### Using curl with Token

```bash
TOKEN="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."

curl -X POST http://localhost:8000/api/v1/rag/query \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"query": "What is the topic?"}'
```

### Using curl with Environment Variable

```bash
export RAG_TOKEN="your_access_token_here"

curl -X POST http://localhost:8000/api/v1/rag/query \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $RAG_TOKEN" \
  -d '{"query": "What is the topic?"}'
```

### Using Python Requests

```python
import requests

BASE_URL = "http://localhost:8000/api/v1"
token = "your_access_token_here"

headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json"
}

# Query endpoint
response = requests.post(
    f"{BASE_URL}/rag/query",
    headers=headers,
    json={"query": "What is the main topic?"}
)
result = response.json()
print(f"Answer: {result['answer']}")
print(f"Sources: {len(result['sources'])} chunks")

# Health check
health = requests.post(
    f"{BASE_URL}/rag/health",
    headers=headers
)
print(f"Status: {health.json()['status']}")
```

### Using JavaScript/Node.js

```javascript
const BASE_URL = "http://localhost:8000/api/v1";
const token = "your_access_token_here";

const headers = {
  "Authorization": `Bearer ${token}`,
  "Content-Type": "application/json"
};

// Query
fetch(`${BASE_URL}/rag/query`, {
  method: "POST",
  headers,
  body: JSON.stringify({ query: "What is the main topic?" })
})
.then(res => res.json())
.then(data => {
  console.log("Answer:", data.answer);
  console.log("Sources:", data.sources.length);
});

// Health check
fetch(`${BASE_URL}/rag/health`, {
  method: "POST",
  headers
})
.then(res => res.json())
.then(data => console.log("Status:", data.status));
```

---

## Status Codes

| Code | Status | Description |
|------|--------|-------------|
| 200 | OK | Request successful |
| 202 | Accepted | Request accepted, processing async |
| 400 | Bad Request | Invalid request format or parameters |
| 401 | Unauthorized | Missing or invalid authentication token |
| 429 | Too Many Requests | Rate limit exceeded |
| 500 | Internal Server Error | Server error during processing |
| 503 | Service Unavailable | Vector store or LLM service unavailable |

---

## Configuration Reference

These settings affect RAG behavior:

| Setting | Default | Description |
|---------|---------|-------------|
| `RAG_CHUNK_SIZE` | 500 | Tokens per document chunk |
| `RAG_CHUNK_OVERLAP` | 50 | Token overlap between chunks |
| `RAG_SEARCH_TOP_K` | 5 | Number of chunks to retrieve |
| `PINECONE_API_KEY` | (required) | Pinecone vector database key |
| `ANTHROPIC_API_KEY` | (required) | Claude API key for answers |

Set these in `.env` file.

---

## Performance Characteristics

### Latency

| Operation | Typical Time |
|-----------|--------------|
| Health check | <100ms |
| Query embedding | 200-500ms |
| Vector search | 100-300ms |
| Claude answer | 1-3 seconds |
| **Total Q&A** | **1.5-4 seconds** |

### Throughput

- **Sequential:** ~15 Q&A per minute (under 20 req/min rate limit)
- **Parallelizable:** Multiple clients per second (at API limit)

### Storage

- Per document: ~1 MB for 50k token doc in Pinecone
- Metadata: ~200 bytes per chunk

---

## Limits & Quotas

| Resource | Limit | Notes |
|----------|-------|-------|
| Query length | No limit | Must be ≥3 chars |
| Response tokens | 1024 | Max tokens in Claude answer |
| Retrievable chunks | 5 | Top K for vector search |
| Rate limit | 20/min | Per IP address |
| Document size | Unlimited | No per-document limit |
| Total vectors | Pinecone plan | Depends on subscription |

---

## Errors & Troubleshooting

### Query Returns Empty Answer

**Cause:** No documents indexed or query doesn't match content

**Solution:** 
1. Upload and index documents first
2. Try more specific queries
3. Check document preview in vector store

### "RAG system unavailable"

**Cause:** Pinecone API key missing or invalid

**Solution:**
1. Verify `PINECONE_API_KEY` in `.env`
2. Check key is valid at https://pinecone.io
3. Verify network connectivity

### "Rate limit exceeded"

**Cause:** Too many requests in 1 minute

**Solution:**
1. Wait 60 seconds
2. Space requests over time
3. Implement client-side rate limiting

### Slow Responses (>5 seconds)

**Cause:** High Pinecone latency or Claude processing

**Solution:**
1. Check Pinecone index status
2. Monitor Claude API status
3. Reduce chunk size if too many docs

---

## Best Practices

1. **Token Reuse:** Keep tokens fresh, refresh before expiry
2. **Error Handling:** Implement retry logic for rate limits
3. **Caching:** Cache frequent queries at application level
4. **Batch:** For multiple queries, space them 3+ seconds apart
5. **Monitoring:** Log query times and error rates
6. **Testing:** Start with health check before queries
