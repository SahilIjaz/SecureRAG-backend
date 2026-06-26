# Quick Start: PDF Upload & Web Scraping

## 🚀 Setup (2 minutes)

### Step 1: Install Dependencies
```bash
pip install -r requirements.txt
```

### Step 2: Update Database
```bash
# Add documents table and sample documents table
psql -d securerag < migrations/add_documents_table.sql
```

### Step 3: Configure .env
```bash
# Copy the example
cp .env.example .env

# Edit .env and add Cloudinary credentials:
CLOUDINARY_CLOUD_NAME=your_cloud_name
CLOUDINARY_API_KEY=your_api_key
CLOUDINARY_API_SECRET=your_api_secret

# Web scraping is enabled by default, adjust if needed:
CRAWL4AI_ENABLED=true
CRAWL4AI_TIMEOUT=30
```

### Step 4: Restart Server
```bash
python -m uvicorn app.main:app --reload
```

---

## 📄 API Usage

### Upload PDF File
```bash
curl -X POST http://localhost:8000/api/v1/documents/upload \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -F "files=@document.pdf"
```

**Response:**
```json
{
  "message": "1 document(s) uploaded successfully.",
  "documents": [{
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "original_filename": "document.pdf",
    "file_size_mb": 2.5,
    "source": "uploaded",
    "status": "pending",
    "file_url": "https://res.cloudinary.com/...",
    "created_at": "2026-06-26T12:00:00+00:00"
  }],
  "total_count": 1,
  "total_storage_mb": 2.5
}
```

---

### Scrape Website
```bash
curl -X POST http://localhost:8000/api/v1/documents/scrape \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "urls": [
      "https://example.com/article",
      "https://blog.example.com/post"
    ]
  }'
```

**Response:**
```json
{
  "message": "2 website(s) scraped and added successfully.",
  "documents": [{
    "id": "550e8400-e29b-41d4-a716-446655440001",
    "original_filename": "Example Article Title",
    "file_size_mb": 0.8,
    "source": "scraped",
    "source_url": "https://example.com/article",
    "status": "pending",
    "file_url": "https://res.cloudinary.com/...",
    "created_at": "2026-06-26T12:00:00+00:00"
  }],
  "total_count": 2,
  "total_storage_mb": 1.6
}
```

---

### List All Documents
```bash
curl -X GET http://localhost:8000/api/v1/documents/ \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

---

## 📊 Quota Limits

| Plan | Max Docs | Max File Size |
|------|----------|---------------|
| Free | 10 | 15 MB |
| Pro | 100 | 50 MB |
| Pro+ | Unlimited | Unlimited |

**Note:** Both uploads and scraped documents count toward quotas.

---

## ✅ What Works

✓ Upload PDF files (up to 50MB)  
✓ Scrape websites using Crawl4AI  
✓ Convert web content to PDF  
✓ Store on Cloudinary  
✓ Multi-tenant isolation  
✓ Quota enforcement  
✓ Usage tracking  
✓ Community document sharing  

---

## ❌ Known Limitations

- ❌ Only PDF files supported (no DOCX, DOC, TXT anymore)
- ❌ PDF generation runs in executor (not fully async)
- ❌ URLs processed sequentially, not in parallel
- ❌ No automatic retry for failed scrapes
- ❌ No background job queue (everything waits for response)

---

## 🐛 Troubleshooting

### Error: "File type not allowed"
→ Only PDF files are accepted. Convert DOCX to PDF first.

### Error: "exceeds the 50MB per-file limit"
→ File is too large. Split into smaller PDFs or upgrade plan.

### Error: "Document quota exceeded"
→ Delete old documents or upgrade subscription plan.

### Error: "Failed to scrape https://..."
→ Website might be unreachable or blocking scraper. Try different URL.

### Error: "Web scraping is not enabled"
→ Set `CRAWL4AI_ENABLED=true` in `.env` and restart server.

### Error: "Cloudinary upload failed"
→ Check Cloudinary credentials in `.env`

---

## 📚 Documentation

- **Feature Guide**: `FEATURE_DOCUMENT_UPLOAD_SCRAPING.md`
- **Implementation Details**: `IMPLEMENTATION_SUMMARY.md`
- **API Docs**: Swagger at `http://localhost:8000/docs`

---

## 🧪 Testing

### Test with curl
```bash
# Get token first
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/signin \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com","password":"password"}' \
  | jq -r '.access_token')

# Upload
curl -X POST http://localhost:8000/api/v1/documents/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@test.pdf"

# Scrape
curl -X POST http://localhost:8000/api/v1/documents/scrape \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"urls": ["https://github.com"]}'
```

### Test with Swagger UI
1. Open `http://localhost:8000/docs`
2. Click "Authorize" and enter your token
3. Test endpoints interactively

---

## 🔧 Configuration

Edit `.env`:

```env
# File upload (max size in MB)
MAX_UPLOAD_SIZE_MB=50

# Web scraping
CRAWL4AI_ENABLED=true
CRAWL4AI_TIMEOUT=30
CRAWL4AI_MAX_CONTENT_SIZE_MB=50

# Cloudinary
CLOUDINARY_CLOUD_NAME=your_cloud_name
CLOUDINARY_API_KEY=your_api_key
CLOUDINARY_API_SECRET=your_api_secret
```

---

## 📈 What Happens Next

1. **Document Received**: Status = `pending`
2. **Processing**: Status = `processing` (by RAG pipeline)
3. **Ready**: Status = `ready` (available for Q&A)
4. **Failed**: Status = `failed` (if processing error)

Note: Current backend doesn't auto-transition. A separate pipeline service handles this.

---

## 🎯 Examples

### Upload Multiple PDFs
```bash
curl -X POST http://localhost:8000/api/v1/documents/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@doc1.pdf" \
  -F "files=@doc2.pdf" \
  -F "files=@doc3.pdf"
```

### Scrape Multiple URLs
```bash
curl -X POST http://localhost:8000/api/v1/documents/scrape \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "urls": [
      "https://github.com/unclecode/crawl4ai",
      "https://medium.com/@example/article",
      "https://blog.example.com/post"
    ]
  }'
```

### Check Usage
```bash
curl -X GET http://localhost:8000/api/v1/documents/ \
  -H "Authorization: Bearer $TOKEN" | jq '.total_storage_mb'
```

---

## ⚡ Performance Tips

- **Batch uploads**: Upload multiple PDFs in one request
- **Timeout**: Increase `CRAWL4AI_TIMEOUT` for slow websites
- **Storage**: Check `total_storage_mb` to stay under quota
- **Scraping**: Large websites may take longer to scrape

---

## 🚨 Before Going to Production

1. ✅ Set Cloudinary credentials
2. ✅ Run database migration
3. ✅ Test upload with real PDF
4. ✅ Test scraping with real URL
5. ✅ Verify quota enforcement works
6. ✅ Check multi-tenant isolation
7. ✅ Monitor error rates
8. ✅ Review Cloudinary cost estimates

---

## 📞 Need Help?

1. Check `FEATURE_DOCUMENT_UPLOAD_SCRAPING.md`
2. Review error messages in API response
3. Check server logs: `print()` statements or logs
4. Test with Swagger UI: `http://localhost:8000/docs`
5. Verify `.env` configuration

---

**Last Updated**: 2026-06-26  
**Status**: Production Ready  
**Questions?** Check the feature documentation
