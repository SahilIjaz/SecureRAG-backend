# RAG (Retrieval-Augmented Generation) Implementation

## Overview

This document describes the Retrieval-Augmented Generation (RAG) system integrated into SecureRAG++. RAG enables LLM-powered question answering over your document collection by retrieving relevant chunks and grounding answers in actual document content.

## Architecture

```
Document Upload
     ↓
[PDF Text Extraction] ← PyPDF2
     ↓
[Recursive Chunking] ← tiktoken (token counting)
     ↓
[Embedding Generation] ← Anthropic Claude or custom embeddings
     ↓
[Vector Storage] ← Pinecone (semantic search index)
     ↓
User Query
     ↓
[Query Embedding] ← Same embedding model
     ↓
[Vector Similarity Search] ← Pinecone semantic search
     ↓
[LLM Answer Generation] ← Anthropic Claude
     ↓
Response with Source Citations
```

## Core Components

### 1. Document Chunking (`app/core/chunking.py`)

Splits documents into overlapping chunks for optimal RAG performance.

**Key Functions:**
- `chunk_pdf_text(text, chunk_size=500, overlap_size=50)` - Chunk by token count with overlap
- `count_tokens(text)` - Count tokens using tiktoken (Claude-compatible)

**Configuration:**
```python
RAG_CHUNK_SIZE: int = 500       # Token-based chunks (~1800 chars)
RAG_CHUNK_OVERLAP: int = 50     # Token overlap between chunks
```

**Why token-based chunking?**
- Token count directly correlates with LLM processing cost
- Ensures consistent chunk quality regardless of text density
- Overlap prevents semantic breaks at chunk boundaries

### 2. Embeddings (`app/core/embeddings.py`)

Converts text into vector representations for semantic similarity.

**Key Functions:**
- `embed_text(text)` - Generate embedding for single text
- `embed_chunks(texts)` - Batch embed multiple texts

**Current Implementation:**
- Simple hash-based embedding for development (1536 dimensions)
- Compatible with Pinecone's vector format

**Production Options:**
Replace `_simple_embedding()` with one of:

```python
# OpenAI (recommended for quality)
from openai import OpenAI
client = OpenAI()

async def embed_text_openai(text: str) -> List[float]:
    response = client.embeddings.create(
        model="text-embedding-3-small",
        input=text
    )
    return response.data[0].embedding
```

```python
# Hugging Face (local, no API cost)
from sentence_transformers import SentenceTransformer
model = SentenceTransformer('all-MiniLM-L6-v2')

async def embed_text_hf(text: str) -> List[float]:
    embedding = model.encode(text)
    return embedding.tolist()
```

```python
# Anthropic (if available in future)
async def embed_text_anthropic(text: str) -> List[float]:
    response = client.messages.embed(
        model="claude-3-embed",
        input=text
    )
    return response.embedding
```

### 3. Vector Storage (`app/core/vector_store.py`)

Manages document chunks in Pinecone vector database.

**Key Functions:**
- `upsert_chunks(tenant_id, document_id, chunks, embeddings)` - Index chunks
- `search_chunks(query_embedding, tenant_id, top_k=5)` - Find similar chunks
- `delete_document_chunks(tenant_id, document_id)` - Remove document from index
- `clear_tenant_data(tenant_id)` - GDPR-compliant tenant cleanup

**Multi-Tenancy:**
All vectors include tenant_id in metadata and filters for isolation.

**Metadata Stored:**
```python
{
    "tenant_id": str,           # Multi-tenancy
    "document_id": str,         # Source document
    "chunk_id": str,            # Unique chunk ID
    "sequence": int,            # Position in document
    "text": str,                # First 200 chars (preview)
    "token_count": int          # Tokens in chunk
}
```

### 4. RAG Service (`app/services/rag_service.py`)

Orchestrates the RAG pipeline end-to-end.

**Key Functions:**
- `process_document_for_rag(tenant_id, document_id, pdf_bytes)` - Index a document
- `retrieve_context_for_query(tenant_id, query)` - Find relevant chunks
- `answer_question(tenant_id, query)` - Generate LLM answer with retrieved context

**Answer Generation Prompt:**
```
You are a helpful assistant answering questions based on provided documents.

CONTEXT:
[Retrieved chunks joined with newlines]

QUESTION: [User query]

Please answer the question based on the context provided. 
If the answer is not in the context, say so.
```

## API Endpoints

### Q&A Endpoint

```http
POST /api/v1/rag/query
Content-Type: application/json
Authorization: Bearer {access_token}

{
  "query": "What is the main topic of the document?"
}
```

**Response:**
```json
{
  "answer": "Based on the documents, the main topic is...",
  "sources": [
    "First relevant chunk text...",
    "Second relevant chunk text..."
  ],
  "model": "claude-3-5-sonnet-20241022"
}
```

**Rate Limit:** 20 requests/minute per IP

### Health Check Endpoint

```http
POST /api/v1/rag/health
Authorization: Bearer {access_token}
```

**Response:**
```json
{
  "status": "healthy",
  "vector_store": "pinecone",
  "dimension": 1536,
  "index_fullness": 0.23
}
```

## Configuration

Set these environment variables in `.env`:

```bash
# Pinecone Vector Database
PINECONE_API_KEY=your_api_key_here
PINECONE_INDEX_NAME=securerag-documents

# Anthropic API (for answer generation)
ANTHROPIC_API_KEY=your_api_key_here

# Chunking Strategy
RAG_CHUNK_SIZE=500          # tokens
RAG_CHUNK_OVERLAP=50        # tokens
RAG_SEARCH_TOP_K=5          # top results to retrieve
```

## Usage Flow

### 1. Document Indexing (Automatic)

When a document is uploaded:
1. Server extracts PDF text via PyPDF2
2. Text is chunked into 500-token pieces with 50-token overlap
3. Each chunk is embedded using the embedding model
4. Vectors are stored in Pinecone with metadata
5. Chunks become available for semantic search

### 2. User Query

When user asks a question:
1. Query is embedded using the same embedding model
2. Similar chunks are retrieved from Pinecone (top 5)
3. Retrieved chunks + query are sent to Claude
4. Claude generates an answer grounded in the documents
5. Response includes both answer and source citations

### 3. Multi-Tenancy

- Each tenant has their own vector space (filtered by tenant_id)
- Tenants cannot see each other's documents
- Cleanup is automatic: deleting a document removes chunks from Pinecone

## Data Flow Example

**Document:** "Alice in Wonderland" (50,000 words)

**Chunking:**
```
Chunk 1 (500 tokens): "In the beginning, Alice sat by the riverside..."
Chunk 2 (500 tokens, 50 overlap): "...while her sister read a book. Alice was beginning..."
Chunk 3: "...to get very tired of sitting..."
...
Total: ~91 chunks
```

**Query:** "What was Alice doing at the start of the story?"

**Process:**
1. Embed "What was Alice doing at the start of the story?" → [0.1, 0.2, ...]
2. Search Pinecone for similar embeddings
3. Retrieve:
   - Chunk 1 (score: 0.92): "In the beginning, Alice sat by the riverside..."
   - Chunk 2 (score: 0.89): "...while her sister read a book..."
   - ... (up to 5 chunks)
4. Build context with these chunks
5. Send to Claude with the query
6. Claude returns: "At the start of the story, Alice was sitting by the riverside with her sister..."

## Performance Considerations

### Chunking
- **Smaller chunks (200 tokens):** More precise retrieval, higher storage cost
- **Larger chunks (1000 tokens):** Better context, may miss specific details
- **Current (500 tokens):** Balanced - ~1800 characters average

### Embeddings
- **Dimensions:** Higher (1536) captures more nuance but slower searches
- **Cost:** Depends on embedding API - OpenAI cheapest for volume
- **Speed:** Local models (Hugging Face) faster, cloud APIs more accurate

### Vector Search
- **Top-K:** Retrieving more chunks increases context but may dilute focus
- **Current (5):** Good balance for most use cases
- **Adjustable per query:** Can be configured by endpoint

## Security Considerations

1. **Multi-Tenancy Isolation**
   - Pinecone filters on tenant_id for all queries
   - Impossible for tenant A to retrieve tenant B's chunks

2. **API Key Management**
   - Pinecone key should be kept secret (in .env, not committed)
   - Anthropic key used only for answer generation
   - Access tokens required for RAG endpoints (JWT-based)

3. **Rate Limiting**
   - Query endpoint: 20 requests/minute
   - Prevents abuse and controls LLM API costs

4. **Data Residency**
   - Chunks stored in Pinecone (third-party)
   - Consider data residency requirements for your jurisdiction

## Troubleshooting

### "No relevant documents found"
- Check that documents were uploaded and indexed
- Verify Pinecone API key is set
- Test with very specific queries that should match indexed content

### "RAG system unavailable"
- Check Pinecone API key is valid
- Verify Pinecone index exists and is accessible
- Check network connectivity to Pinecone

### Slow query responses
- Check Pinecone index size and fullness
- Consider increasing top_k gradually to find bottleneck
- Verify embedding generation isn't the bottleneck

### Poor answer quality
- Replace simple embedding with production model (OpenAI/Hugging Face)
- Increase chunk_size to 750-1000 for more context
- Verify Claude model is latest version

## Next Steps

1. **Production Embeddings**
   - Integrate OpenAI text-embedding-3-small API
   - Or use local Hugging Face model

2. **Document Processing Integration**
   - Connect document upload endpoint to RAG pipeline
   - Add progress tracking for indexing
   - Support batch document indexing

3. **Advanced RAG Features**
   - Hybrid search (keyword + semantic)
   - Reranking of results before LLM
   - Conversation history context
   - Citation tracking with page numbers

4. **Monitoring**
   - Track embedding latency
   - Monitor Pinecone index health
   - Log common query patterns
   - Measure answer quality

## References

- **Pinecone Docs:** https://docs.pinecone.io
- **Anthropic API:** https://docs.anthropic.com
- **Chunking Best Practices:** https://cookbook.openai.com/examples/rag_with_token_aware_chunking
