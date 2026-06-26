# RAG Quick Start Guide

## Server Status ✅

The server is running and all RAG endpoints are active:
- `POST /api/v1/rag/query` - Ask questions
- `POST /api/v1/rag/health` - Check vector store
- `POST /api/v1/rag/process-document/{id}` - Index documents

## 1. Get Authentication Token

First, sign up or sign in to get a JWT token:

```bash
# Sign up
curl -X POST http://localhost:8000/api/v1/auth/signup \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "password": "SecurePassword123!",
    "company_name": "My Company"
  }'
```

Response:
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "...",
  "token_type": "bearer",
  "user": {
    "id": "xxx",
    "email": "test@example.com"
  }
}
```

**Save the `access_token` for the next step.**

## 2. Check RAG System Health

Before asking questions, verify the vector store is accessible:

```bash
TOKEN="your_access_token_here"

curl -X POST http://localhost:8000/api/v1/rag/health \
  -H "Authorization: Bearer $TOKEN"
```

Expected response (if Pinecone API key is configured):
```json
{
  "status": "healthy",
  "vector_store": "pinecone",
  "dimension": 1536,
  "index_fullness": 0.05
}
```

**Note:** If you see "RAG system unavailable", ensure `PINECONE_API_KEY` is set in `.env`.

## 3. Ask a Question

Once you have documents indexed, ask questions:

```bash
TOKEN="your_access_token_here"

curl -X POST http://localhost:8000/api/v1/rag/query \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "query": "What is the main topic of the documents?"
  }'
```

Response:
```json
{
  "answer": "Based on the documents, the main topic is... [Claude's analysis]",
  "sources": [
    "First relevant chunk from document...",
    "Second relevant chunk from document...",
    "Third relevant chunk from document..."
  ],
  "model": "claude-3-5-sonnet-20241022"
}
```

## Configuration

Make sure these are set in `.env`:

```bash
# Pinecone Vector Database
PINECONE_API_KEY=pk_xxxxx              # Get from https://pinecone.io
PINECONE_INDEX_NAME=securerag-documents

# Anthropic API (for Q&A)
ANTHROPIC_API_KEY=sk-ant-xxxxx         # Get from https://console.anthropic.com

# RAG Settings (already configured)
RAG_CHUNK_SIZE=500                     # tokens per chunk
RAG_CHUNK_OVERLAP=50                   # token overlap
RAG_SEARCH_TOP_K=5                     # retrieve top 5 chunks
```

## How It Works

1. **Query comes in** → Must have valid JWT token
2. **Query is embedded** → Converted to vector using embedding model
3. **Vector search in Pinecone** → Finds similar document chunks
4. **Chunks retrieved** → Top 5 most relevant chunks selected
5. **Answer generated** → Claude creates answer from context
6. **Sources returned** → Users see which document chunks were used

## Rate Limits

- **Query endpoint:** 20 requests per minute per IP
- **Health endpoint:** 10 requests per minute per IP

Once limit is exceeded, you get a 429 (Too Many Requests) response.

## Example Query Patterns

### Topic Summary
```json
{
  "query": "What is the main topic of the documents?"
}
```

### Specific Information Lookup
```json
{
  "query": "What security measures are mentioned?"
}
```

### Comparison Question
```json
{
  "query": "How do the approaches in different documents compare?"
}
```

### Definition/Explanation
```json
{
  "query": "Explain the technical process described in the documents"
}
```

## Troubleshooting

### "RAG system unavailable"
- Check `PINECONE_API_KEY` is set and valid
- Verify Pinecone index exists
- Check network connectivity

### "No relevant documents found"
- This is normal if no documents are indexed yet
- Upload and index documents first
- Try queries that match document content

### "Rate limit exceeded"
- Wait 1 minute before making another request
- Or spread requests across different times

### "Invalid auth token"
- Token may have expired (30 min default)
- Get a fresh token by signing in again
- Ensure token is in Authorization header: `Bearer {token}`

## Performance

Typical response times:
- Health check: <100ms
- Q&A with answer: 1.5-4 seconds (depends on Claude response time)

## Next Steps

1. Upload documents (PDF or web scrape)
2. Documents are automatically indexed for RAG
3. Start asking questions!

See `RAG_IMPLEMENTATION.md` for detailed architecture and advanced topics.
