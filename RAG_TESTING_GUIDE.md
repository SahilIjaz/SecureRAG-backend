# RAG Testing Guide

## Prerequisites

1. **API Keys Set in `.env`:**
   ```bash
   PINECONE_API_KEY=your_api_key_here
   ANTHROPIC_API_KEY=your_api_key_here
   ```

2. **Server Running:**
   ```bash
   python -m uvicorn app.main:app --reload
   ```

3. **Auth Token Required:**
   Get a valid JWT token by signing in first.

## Testing the RAG System

### 1. Sign Up / Get Auth Token

```bash
curl -X POST http://localhost:8000/api/v1/auth/signup \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "password": "SecurePassword123!",
    "company_name": "Test Company"
  }'
```

Response:
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "user": {
    "id": "123e4567-e89b-12d3-a456-426614174000",
    "email": "test@example.com",
    "is_verified": false
  }
}
```

Save the `access_token` for subsequent requests.

### 2. Verify Email (if required)

Skip if not enforced, or check `/api/v1/auth/verify-email` with OTP.

### 3. Check RAG Health

Verify the vector store is accessible:

```bash
curl -X POST http://localhost:8000/api/v1/rag/health \
  -H "Authorization: Bearer {access_token}"
```

Expected response (if Pinecone API key is set):
```json
{
  "status": "healthy",
  "vector_store": "pinecone",
  "dimension": 1536,
  "index_fullness": 0.12
}
```

### 4. Query Documents (Q&A)

Ask a question about your indexed documents:

```bash
curl -X POST http://localhost:8000/api/v1/rag/query \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer {access_token}" \
  -d '{
    "query": "What is the main topic of the documents?"
  }'
```

Response:
```json
{
  "answer": "Based on the documents provided, the main topic is... [Claude's response]",
  "sources": [
    "First retrieved chunk with relevant information...",
    "Second retrieved chunk with relevant information...",
    "Third retrieved chunk with relevant information..."
  ],
  "model": "claude-3-5-sonnet-20241022"
}
```

### 5. Test with Different Query Types

#### Technical Query
```json
{
  "query": "How does the system handle authentication?"
}
```

#### Specific Fact Query
```json
{
  "query": "What is mentioned about encryption?"
}
```

#### Multi-Part Query
```json
{
  "query": "What are the key components and how do they interact?"
}
```

#### Edge Case: No Matching Documents
```json
{
  "query": "Tell me about ancient Roman architecture"
}
```

Expected: "No relevant documents found for your question."

## Testing with Postman

### 1. Create Collection: "SecureRAG RAG Tests"

### 2. Set Collection Variables:
```
{{base_url}} = http://localhost:8000
{{access_token}} = (set after signup)
{{user_id}} = (set after signup)
```

### 3. Create Requests:

**Request 1: Sign Up**
```
POST {{base_url}}/api/v1/auth/signup
Body (raw JSON):
{
  "email": "test@example.com",
  "password": "SecurePassword123!",
  "company_name": "Test Company"
}

Tests Tab:
var jsonData = pm.response.json();
pm.environment.set("access_token", jsonData.access_token);
pm.environment.set("user_id", jsonData.user.id);
```

**Request 2: RAG Health Check**
```
POST {{base_url}}/api/v1/rag/health
Headers:
  Authorization: Bearer {{access_token}}

Tests Tab:
pm.test("Status is healthy or error", function() {
  var jsonData = pm.response.json();
  pm.expect(jsonData.status).to.be.oneOf(["healthy"]);
});
```

**Request 3: RAG Query - Basic**
```
POST {{base_url}}/api/v1/rag/query
Headers:
  Authorization: Bearer {{access_token}}
  Content-Type: application/json

Body (raw JSON):
{
  "query": "What is the main topic of the documents?"
}

Tests Tab:
pm.test("Response has answer and sources", function() {
  var jsonData = pm.response.json();
  pm.expect(jsonData).to.have.property("answer");
  pm.expect(jsonData).to.have.property("sources");
  pm.expect(jsonData).to.have.property("model");
});

pm.test("Sources is an array", function() {
  var jsonData = pm.response.json();
  pm.expect(Array.isArray(jsonData.sources)).to.equal(true);
});
```

**Request 4: RAG Query - Rate Limit Test**
```
Run in a loop to test 20 req/minute rate limit:

for (let i = 0; i < 25; i++) {
  pm.sendRequest({
    url: "{{base_url}}/api/v1/rag/query",
    method: "POST",
    header: {
      "Authorization": "Bearer {{access_token}}",
      "Content-Type": "application/json"
    },
    body: {
      mode: "raw",
      raw: JSON.stringify({
        "query": "What is mentioned?"
      })
    }
  }, function(err, res) {
    if (i >= 20) {
      pm.test(`Request ${i+1} rate limited`, function() {
        pm.expect(res.code).to.equal(429);
      });
    }
  });
}
```

## Testing Scenarios

### Scenario 1: Basic Functionality
1. Sign up
2. Check RAG health
3. Ask a simple question
4. Verify answer includes sources

### Scenario 2: Quality Testing
1. Sign up
2. Upload a sample PDF document
3. Ask specific questions about content
4. Verify answers are accurate
5. Check source citations match retrieved chunks

### Scenario 3: Error Handling
1. Test with invalid/empty query
2. Test with very long query (1000+ chars)
3. Test without auth token
4. Test with rate limiting
5. Test when Pinecone API key is missing

### Scenario 4: Multi-Tenant Isolation
1. Create User A, sign in, ask query
2. Create User B, sign in, ask same query
3. Verify User B gets different/no results (if User A has indexed docs)
4. Verify User A cannot see User B's documents

## Example Test Script (Python)

```python
import requests
import json

BASE_URL = "http://localhost:8000"
EMAIL = "test@example.com"
PASSWORD = "SecurePassword123!"

# Step 1: Sign up
signup_response = requests.post(
    f"{BASE_URL}/api/v1/auth/signup",
    json={
        "email": EMAIL,
        "password": PASSWORD,
        "company_name": "Test Company"
    }
)
assert signup_response.status_code == 200
token = signup_response.json()["access_token"]
print(f"✓ Signed up and got token: {token[:20]}...")

# Step 2: Check RAG health
health_response = requests.post(
    f"{BASE_URL}/api/v1/rag/health",
    headers={"Authorization": f"Bearer {token}"}
)
if health_response.status_code == 200:
    print(f"✓ RAG Health: {health_response.json()}")
else:
    print(f"⚠ RAG unavailable: {health_response.json()}")

# Step 3: Query
query_response = requests.post(
    f"{BASE_URL}/api/v1/rag/query",
    headers={"Authorization": f"Bearer {token}"},
    json={"query": "What is the main topic?"}
)
assert query_response.status_code == 200
result = query_response.json()
print(f"✓ Query returned:")
print(f"  Answer: {result['answer'][:100]}...")
print(f"  Sources: {len(result['sources'])} chunks retrieved")
print(f"  Model: {result['model']}")
```

## Troubleshooting

### "RAG system unavailable"
- Check `PINECONE_API_KEY` is set correctly in `.env`
- Verify Pinecone index exists and is accessible
- Check network connectivity to api.pinecone.io

### "No module named 'pinecone'"
- Run: `pip install pinecone-client==5.0.1`

### "No relevant documents found"
- This is expected if no documents have been indexed yet
- Upload a PDF first, then index it
- Try queries that match document content

### "Rate limit exceeded"
- Wait 1 minute or use different API key
- This is intentional to prevent abuse

### "Invalid auth token"
- Get a fresh token by signing up/signing in
- Verify token is passed in Authorization header

## Performance Benchmarks

Typical latencies (with Pinecone and Anthropic APIs):
- RAG health check: <100ms
- Query embedding: 200-500ms
- Vector search: 100-300ms
- Claude answer generation: 1-3 seconds
- **Total Q&A time: 1.5-4 seconds**

Varies based on:
- Document corpus size
- Network latency
- Claude model availability
- Query complexity
