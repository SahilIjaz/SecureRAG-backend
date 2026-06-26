# Document Upload & Web Scraping Feature

## Overview

SecureRAG now supports two ways to add documents to your workspace:

1. **Direct PDF Upload** - Upload PDF files up to 50MB each
2. **Web Scraping** - Scrape website content and automatically convert to PDF

Both features respect tenant quotas, storage limits, and multi-tenant isolation.

---

## 1. PDF File Upload

### Requirements

- **File Format**: PDF only (MIME type: `application/pdf`)
- **File Size**: Up to 50MB per file (configurable via quota)
- **Quantity**: Limited by tenant's document quota
  - Free plan: 10 documents max
  - Pro plan: 100 documents max
  - Pro+ plan: Unlimited documents

### Upload Flow

```
1. Frontend sends POST /api/v1/documents/upload
   - Multipart form data with files[] array
   - Requires authentication (access token or onboarding token)

2. Backend validates:
   - File type is PDF
   - File size ≤ max_file_size_mb (from quota or config)
   - Total documents after upload ≤ max_documents quota
   - Total storage after upload ≤ max_storage (if quota exists)

3. For each valid file:
   - Upload to Cloudinary (cloud storage)
   - Create Document record in database
   - Mark as source="uploaded", status="pending"
   - Update UsageCount (documents_count, storage_used_mb)

4. Return DocumentsResponse with uploaded documents
```

### API Endpoint

**POST** `/api/v1/documents/upload`

**Request:**
```bash
curl -X POST http://localhost:8000/api/v1/documents/upload \
  -H "Authorization: Bearer <access_token>" \
  -F "files=@document1.pdf" \
  -F "files=@document2.pdf"
```

**Response:**
```json
{
  "message": "2 document(s) uploaded successfully.",
  "documents": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "original_filename": "document1.pdf",
      "file_size_mb": 2.5,
      "mime_type": "application/pdf",
      "source": "uploaded",
      "status": "pending",
      "sample_document_id": null,
      "source_url": null,
      "file_url": "https://res.cloudinary.com/...",
      "created_at": "2026-06-26T12:00:00+00:00"
    }
  ],
  "total_count": 2,
  "total_storage_mb": 5.0
}
```

### Database Schema

```sql
-- Document record created for each uploaded file
INSERT INTO documents (
    tenant_id,
    original_filename,
    file_path,              -- Cloudinary public_id
    file_url,               -- HTTPS URL
    file_size_mb,
    mime_type,
    source,                 -- "uploaded"
    status,                 -- "pending" (waiting for processing)
    is_active
) VALUES (...)
```

---

## 2. Web Scraping with Crawl4AI

### Requirements

- **URLs**: Must start with `http://` or `https://`
- **Content Limits**: Scraped content converted to PDF must be ≤ 50MB
- **Quota**: Counts toward total document and storage quotas
- **Processing**: Async, non-blocking using Crawl4AI library

### How It Works

1. **User provides URL** → `/api/v1/documents/scrape`
2. **Backend scrapes URL** using Crawl4AI
   - Extracts markdown content
   - Includes page title and metadata
3. **Convert to PDF** using ReportLab
   - Formats markdown content nicely
   - Adds source URL and title
   - Handles large content gracefully
4. **Store on Cloudinary** (same as file uploads)
5. **Create Document record** with source="scraped"

### API Endpoint

**POST** `/api/v1/documents/scrape`

**Request:**
```bash
curl -X POST http://localhost:8000/api/v1/documents/scrape \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "urls": [
      "https://example.com/article",
      "https://blog.example.com/post"
    ]
  }'
```

**Request Schema:**
```json
{
  "urls": [
    "string (URL must start with http:// or https://)"
  ]
}
```

**Response:**
```json
{
  "message": "2 website(s) scraped and added successfully.",
  "documents": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440001",
      "original_filename": "Example Article Title",
      "file_size_mb": 0.8,
      "mime_type": "application/pdf",
      "source": "scraped",
      "status": "pending",
      "sample_document_id": null,
      "source_url": "https://example.com/article",
      "file_url": "https://res.cloudinary.com/...",
      "created_at": "2026-06-26T12:00:00+00:00"
    }
  ],
  "total_count": 2,
  "total_storage_mb": 1.6
}
```

### Error Handling

| Error | Status | Reason |
|-------|--------|--------|
| Invalid URL | 400 | URL doesn't start with http:// or https:// |
| Scraping failed | 400 | Website couldn't be reached or parsed |
| Content too large | 413 | Scraped PDF > max_file_size_mb quota |
| Quota exceeded | 403 | Document count would exceed limit |
| Scraping disabled | 400 | CRAWL4AI_ENABLED=false in config |

### Configuration

Edit `.env`:
```env
# Web scraping settings
CRAWL4AI_ENABLED=true
CRAWL4AI_TIMEOUT=30              # seconds to wait for response
CRAWL4AI_MAX_CONTENT_SIZE_MB=50  # max size after conversion to PDF
```

### Behind the Scenes

#### Crawl4AI Integration

```python
from crawl4ai import AsyncWebCrawler

async with AsyncWebCrawler(timeout=30) as crawler:
    result = await crawler.arun(url)
    # result.markdown - extracted markdown content
    # result.metadata - page title, description, etc.
    # result.success - boolean success flag
```

#### PDF Generation

```python
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

# Creates PDF with:
# 1. Page title (from website <title> tag)
# 2. Source URL (as citation)
# 3. Extracted markdown content (formatted)
# 4. Proper page breaks and margins
```

#### Database Record

```sql
INSERT INTO documents (
    tenant_id,
    original_filename,      -- Page title
    file_path,              -- Cloudinary public_id
    file_url,               -- HTTPS URL to PDF
    file_size_mb,           -- Size after conversion
    mime_type,              -- "application/pdf"
    source,                 -- "scraped"
    source_url,             -- Original website URL
    status,                 -- "pending" (waiting for processing)
    is_active
) VALUES (...)
```

---

## 3. Document Quota Management

### Quota Limits by Plan

| Plan | Max Documents | Max File Size | Storage Limit |
|------|---------------|---------------|---------------|
| **Free** | 10 | 15MB | (depends on total) |
| **Pro** | 100 | 50MB | (depends on total) |
| **Pro+** | Unlimited | Unlimited | Unlimited |

### Quota Validation

Both upload and scrape endpoints validate:

1. **Individual file size**: `file_size_mb ≤ max_file_size_mb`
2. **Document count**: `existing_count + new_count ≤ max_documents`
3. **Total storage**: Tracked in `UsageCount.storage_used_mb`

### Usage Tracking

Monthly usage record per tenant:

```sql
UPDATE usage_counts SET
    documents_count = documents_count + 2,
    storage_used_mb = storage_used_mb + 2.5
WHERE tenant_id = ? AND period_month = '2026-06-01'
```

---

## 4. API Endpoints Summary

| Endpoint | Method | Purpose | Auth |
|----------|--------|---------|------|
| `/documents/upload` | POST | Upload PDF files | Onboarding / Access |
| `/documents/scrape` | POST | Scrape websites | Onboarding / Access |
| `/documents/samples` | GET | List community docs | Onboarding / Access |
| `/documents/select-sample` | POST | Add community doc | Onboarding / Access |
| `/documents/select-platform-samples` | POST | Add platform samples | Onboarding / Access |
| `/documents/` | GET | List all documents | Access |

---

## 5. Document Status Lifecycle

```
Pending → Processing → Ready
              ↓
             Failed
```

- **pending**: File uploaded/scraped, awaiting processing
- **processing**: RAG pipeline is chunking and embedding
- **ready**: Available for Q&A queries
- **failed**: Processing error (file corrupted, etc.)

Note: Current backend doesn't auto-transition states. A separate pipeline service handles this.

---

## 6. Multi-Tenant Isolation

All documents are scoped to `tenant_id`:

```python
# Users can ONLY see their own tenant's documents
documents = await db.execute(
    select(Document).where(
        Document.tenant_id == user.tenant_id,  # Always filtered by tenant
        Document.is_active == True
    )
)

# Cloudinary paths include tenant_id for physical isolation
# securerag/uploads/{tenant_id}/{unique_filename}.pdf
```

---

## 7. Community Document Sharing

Uploaded documents become **samples** for other users in the same business category:

```
User A uploads "document.pdf"
  ↓
Document saved with source="uploaded", business_category="Healthcare"
  ↓
User B in same business category sees it in GET /documents/samples
  ↓
User B can POST /documents/select-sample to copy it to their workspace
  ↓
New Document created in User B's tenant with source="sample"
```

---

## 8. Technical Stack

| Component | Library | Purpose |
|-----------|---------|---------|
| Web Scraping | Crawl4AI | Extract content from websites |
| PDF Generation | ReportLab | Convert markdown to PDF format |
| Cloud Storage | Cloudinary | Host uploaded/scraped PDFs |
| Database | PostgreSQL + SQLAlchemy | Store document metadata |
| API | FastAPI | REST endpoints |

---

## 9. Configuration Reference

### `.env` Variables

```env
# File upload limits
MAX_UPLOAD_SIZE_MB=50                  # Default max file size
ALLOWED_MIME_TYPES=application/pdf    # Only PDF for now

# Web scraping
CRAWL4AI_ENABLED=true                 # Enable scraping feature
CRAWL4AI_TIMEOUT=30                   # Seconds to wait per URL
CRAWL4AI_MAX_CONTENT_SIZE_MB=50       # Max PDF size after conversion

# Cloudinary (required for both features)
CLOUDINARY_CLOUD_NAME=your_cloud_name
CLOUDINARY_API_KEY=your_api_key
CLOUDINARY_API_SECRET=your_api_secret

# Database
DATABASE_URL=postgresql://user:pass@host/db
```

### Python Dependencies

```
crawl4ai==0.4.243           # Web scraping
pypdf==4.3.1                # PDF reading (helper)
reportlab==4.1.5            # PDF generation
cloudinary==1.44.1          # Cloud storage (already had this)
```

---

## 10. Error Scenarios

### Scenario 1: File Too Large

```json
{
  "detail": "'large-file.pdf' exceeds the 50MB per-file limit."
}
```
**Fix**: Split into smaller files or upgrade plan

### Scenario 2: Quota Exceeded

```json
{
  "detail": "Document quota exceeded. Your plan allows 10 documents."
}
```
**Fix**: Delete old documents or upgrade plan

### Scenario 3: Invalid URL

```json
{
  "detail": "Invalid URL: example.com. Must start with http:// or https://"
}
```
**Fix**: Provide full URL with protocol

### Scenario 4: Website Unreachable

```json
{
  "detail": "Failed to scrape https://example.com: Connection timeout"
}
```
**Fix**: Check URL is accessible and not blocked

### Scenario 5: Scraped Content Too Large

```json
{
  "detail": "Content from 'https://example.com' exceeds the 50MB per-file limit after scraping."
}
```
**Fix**: Page has too much content, try different URL

---

## 11. Implementation Details

### Upload Flow (Code Path)

```
POST /documents/upload
  ↓
documents.py: upload_documents()
  ↓
Validate MIME types + file sizes
  ↓
Check quotas (count + storage)
  ↓
storage.py: upload_file_to_cloudinary()
  ↓
Cloudinary API (in executor)
  ↓
Create Document record
  ↓
Update UsageCount
  ↓
Return DocumentsResponse
```

### Scrape Flow (Code Path)

```
POST /documents/scrape
  ↓
documents.py: scrape_and_add_documents()
  ↓
Validate URLs + quotas
  ↓
For each URL:
  scraper.py: scrape_website_to_pdf()
    ↓
    Crawl4AI (async)
    ↓
    reportlab: convert to PDF (in executor)
    ↓
  storage.py: upload_file_to_cloudinary()
    ↓
    Cloudinary API
    ↓
  Create Document record with source="scraped"
  ↓
Update UsageCount
  ↓
Return DocumentsResponse
```

---

## 12. Performance Notes

- **Upload**: Sync operation (waits for Cloudinary response)
- **Scrape**: Async web crawling, but blocking PDF generation
- **Timeout**: 30 seconds per URL (configurable)
- **Concurrency**: Multiple URLs processed sequentially per request

### Future Optimization

- Async PDF generation (separate executor pool)
- Batch scraping with retries
- Webhook notifications on processing completion
- Background job queue for large batches

---

## Testing

### Test Upload

```bash
curl -X POST http://localhost:8000/api/v1/documents/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@/path/to/document.pdf"
```

### Test Scrape

```bash
curl -X POST http://localhost:8000/api/v1/documents/scrape \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"urls": ["https://github.com/unclecode/crawl4ai"]}'
```

### Test List

```bash
curl -X GET http://localhost:8000/api/v1/documents/ \
  -H "Authorization: Bearer $TOKEN"
```

---

## Troubleshooting

### "Scraping is not enabled"
- Check `CRAWL4AI_ENABLED=true` in `.env`
- Restart server after changing env

### "Failed to scrape URL"
- Verify URL is publicly accessible
- Check website allows web scraping (robots.txt)
- Increase `CRAWL4AI_TIMEOUT` if slow site

### "File type not allowed"
- Only PDF files are supported
- Convert DOCX/DOC to PDF first

### "Cloudinary upload failed"
- Verify `CLOUDINARY_CLOUD_NAME`, `API_KEY`, `API_SECRET`
- Check Cloudinary quota isn't exceeded

---

**Last Updated**: 2026-06-26  
**Status**: Ready for production  
**Dependencies**: crawl4ai, reportlab, cloudinary
