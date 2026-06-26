# Deployment Checklist: PDF Upload & Web Scraping

## Pre-Deployment (Do in staging environment first)

### Code & Dependencies
- [ ] Pull latest code with all changes
- [ ] Run `pip install -r requirements.txt` (installs crawl4ai, reportlab, pypdf)
- [ ] Verify no import errors: `python -c "import crawl4ai; import reportlab"`
- [ ] Check all modified files are present:
  - [ ] `app/core/scraper.py`
  - [ ] `app/services/document_service.py`
  - [ ] `app/models/document.py`
  - [ ] `app/api/v1/endpoints/documents.py`
  - [ ] `app/schemas/document.py`
  - [ ] `app/config.py`

### Database
- [ ] Backup current database
  ```bash
  pg_dump securerag > backup_2026_06_26.sql
  ```
- [ ] Run migration to create tables:
  ```bash
  psql -d securerag -f migrations/add_documents_table.sql
  ```
- [ ] Verify tables created:
  ```sql
  \dt documents
  \dt sample_documents
  \d documents
  ```
- [ ] Check enum types:
  ```sql
  SELECT typname FROM pg_type WHERE typname LIKE 'document%';
  ```

### Configuration
- [ ] Copy `.env.example` to `.env` (if not exists)
- [ ] Set Cloudinary credentials in `.env`:
  - [ ] `CLOUDINARY_CLOUD_NAME`
  - [ ] `CLOUDINARY_API_KEY`
  - [ ] `CLOUDINARY_API_SECRET`
- [ ] Verify web scraping settings:
  - [ ] `CRAWL4AI_ENABLED=true`
  - [ ] `CRAWL4AI_TIMEOUT=30` (adjust if needed)
  - [ ] `CRAWL4AI_MAX_CONTENT_SIZE_MB=50`
- [ ] Update file upload settings:
  - [ ] `MAX_UPLOAD_SIZE_MB=50` (changed from 15)
  - [ ] `ALLOWED_MIME_TYPES=application/pdf` (now PDF only)
- [ ] Verify database connection works:
  ```bash
  python -c "from app.database import engine; print(engine)"
  ```

### Testing in Staging

#### Test PDF Upload
- [ ] Create test PDF file (~5MB)
- [ ] Upload via API:
  ```bash
  curl -X POST http://staging:8000/api/v1/documents/upload \
    -H "Authorization: Bearer $TOKEN" \
    -F "files=@test.pdf"
  ```
- [ ] Verify response has correct structure
- [ ] Verify file appears in Cloudinary
- [ ] Verify database record created
- [ ] Verify document appears in GET /documents/

#### Test PDF Upload - Edge Cases
- [ ] Upload small PDF (< 1MB) ✓
- [ ] Upload large PDF (40-50MB) ✓
- [ ] Upload oversized PDF (> 50MB) - should fail with 413 ✓
- [ ] Upload DOCX file - should fail with 415 ✓
- [ ] Upload multiple PDFs in one request ✓
- [ ] Verify file_size_mb is accurate ✓
- [ ] Verify source="uploaded" and status="pending" ✓

#### Test Web Scraping
- [ ] Scrape valid public website:
  ```bash
  curl -X POST http://staging:8000/api/v1/documents/scrape \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"urls": ["https://github.com/unclecode/crawl4ai"]}'
  ```
- [ ] Verify response has DocumentsResponse structure
- [ ] Verify source="scraped" and source_url is set
- [ ] Verify file appears in Cloudinary (as PDF)
- [ ] Verify database record created with source_url
- [ ] Verify document appears in GET /documents/

#### Test Web Scraping - Edge Cases
- [ ] Scrape valid URL ✓
- [ ] Scrape URL without protocol (should fail) ✓
- [ ] Scrape unreachable URL (should fail with detailed error) ✓
- [ ] Scrape website with lots of content ✓
- [ ] Scrape multiple URLs in one request ✓
- [ ] Verify page_title extracted correctly ✓
- [ ] Verify file_size_mb is reasonable ✓

#### Test Quota Enforcement
- [ ] Verify Free plan: max_documents=10
  - [ ] Upload 10 PDFs - succeeds
  - [ ] Upload 11th PDF - fails with 403
- [ ] Verify Pro plan: max_documents=100, max_file_size_mb=50
- [ ] Verify scraped docs count toward quota
- [ ] Verify both uploads + scrapes count together

#### Test Multi-Tenant Isolation
- [ ] Create 2 test users in different tenants
- [ ] Upload doc as User A
- [ ] Verify User B cannot see User A's documents
- [ ] Verify Cloudinary paths include tenant_id

#### Test API Contract
- [ ] GET /documents/ response matches DocumentsResponse schema ✓
- [ ] DocumentResponse includes source_url field ✓
- [ ] POST /documents/upload response has correct structure ✓
- [ ] POST /documents/scrape response has correct structure ✓
- [ ] Swagger UI shows new endpoints ✓

#### Test Error Handling
- [ ] Test 400 errors (invalid input)
  - [ ] Invalid URL format
  - [ ] No files provided
  - [ ] No URLs provided
- [ ] Test 403 errors (forbidden)
  - [ ] Quota exceeded
- [ ] Test 413 errors (entity too large)
  - [ ] File > max_file_size_mb
  - [ ] Scraped content > max_file_size_mb

#### Test Backward Compatibility
- [ ] Existing upload endpoint still works
- [ ] Existing document list endpoint still works
- [ ] Old documents still queryable (source = uploaded or sample)
- [ ] Sample document selection still works

### Performance Testing
- [ ] Upload 10MB file - should complete < 5 seconds
- [ ] Scrape simple website - should complete < 15 seconds
- [ ] Scrape complex website - should complete < 30 seconds
- [ ] List 100 documents - should be < 1 second
- [ ] Monitor memory during scraping - should not exceed 500MB

### Security Testing
- [ ] Verify authenticated endpoints require token
- [ ] Verify onboarding token works with upload
- [ ] Verify access token works with upload
- [ ] Verify expired token is rejected
- [ ] Verify CORS headers are correct
- [ ] SQL injection: Try malicious filename (should be safe)
- [ ] Path traversal: Try ../ in URL (should be safe)

### Monitoring Setup
- [ ] Set up logging for scraping errors
- [ ] Set up alerts for Cloudinary quota
- [ ] Set up alerts for database storage
- [ ] Monitor API response times

### Documentation
- [ ] All documentation files are present:
  - [ ] `FEATURE_DOCUMENT_UPLOAD_SCRAPING.md`
  - [ ] `IMPLEMENTATION_SUMMARY.md`
  - [ ] `QUICK_START_WEB_SCRAPING.md`
  - [ ] `DEPLOYMENT_CHECKLIST.md` (this file)
- [ ] API documentation updated in Swagger
- [ ] Example `.env` file has all new settings
- [ ] README mentions new capabilities

---

## Production Deployment

### Before Going Live
- [ ] All staging tests passed
- [ ] No open TODOs in code
- [ ] Backup taken and verified restorable
- [ ] Rollback plan documented
- [ ] Team notified of changes
- [ ] Git branch merged to main

### Deployment Steps

1. **Prepare Production Environment**
   ```bash
   # SSH into production
   ssh user@production
   
   # Backup database
   pg_dump securerag > backup_$(date +%Y%m%d_%H%M%S).sql
   
   # Verify backup
   psql -l | grep securerag
   ```

2. **Update Code**
   ```bash
   cd /var/www/securerag-backend
   git fetch origin
   git pull origin main
   ```

3. **Install Dependencies**
   ```bash
   pip install -r requirements.txt --upgrade
   ```

4. **Run Database Migration**
   ```bash
   psql -d securerag < migrations/add_documents_table.sql
   
   # Verify
   psql -d securerag -c "\dt documents"
   ```

5. **Update Configuration**
   ```bash
   # Edit .env (if needed)
   nano .env
   
   # Verify critical settings:
   grep CLOUDINARY_ .env
   grep CRAWL4AI_ .env
   ```

6. **Restart Application**
   ```bash
   # Using systemd
   sudo systemctl restart securerag-backend
   
   # Or using supervisor
   sudo supervisorctl restart securerag
   
   # Or using docker
   docker-compose restart backend
   
   # Verify it's running
   curl http://localhost:8000/health
   ```

7. **Smoke Test**
   ```bash
   # Get auth token
   TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/signin \
     -H "Content-Type: application/json" \
     -d '{"email":"test@example.com","password":"..."}' \
     | jq -r '.access_token')
   
   # Test upload endpoint
   curl -X POST http://localhost:8000/api/v1/documents/upload \
     -H "Authorization: Bearer $TOKEN" \
     -F "files=@test.pdf"
   
   # Test scrape endpoint
   curl -X POST http://localhost:8000/api/v1/documents/scrape \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"urls": ["https://github.com"]}'
   
   # Test list endpoint
   curl -X GET http://localhost:8000/api/v1/documents/ \
     -H "Authorization: Bearer $TOKEN"
   ```

### Post-Deployment

- [ ] Monitor error logs for 30 minutes
- [ ] Check database for any connection issues
- [ ] Verify Cloudinary uploads working
- [ ] Test in staging once more (if separate env)
- [ ] Notify team deployment successful
- [ ] Update status page if applicable

### Rollback Plan (if issues arise)

**Option 1: Code Rollback**
```bash
git revert HEAD
git push origin main
sudo systemctl restart securerag-backend
```

**Option 2: Database Rollback**
```bash
# Stop application
sudo systemctl stop securerag-backend

# Restore from backup
psql -d securerag < backup_YYYYMMDD_HHMMSS.sql

# Restart application
sudo systemctl start securerag-backend
```

**Option 3: Full Rollback**
```bash
# Roll back both code and database
git checkout <previous-commit>
psql -d securerag < backup_YYYYMMDD_HHMMSS.sql
git push origin main
sudo systemctl restart securerag-backend
```

---

## Post-Deployment (24 hours)

### Monitoring Checklist
- [ ] No spike in error logs
- [ ] API response times normal
- [ ] Database query performance acceptable
- [ ] Cloudinary quota usage reasonable
- [ ] No user complaints in support
- [ ] Feature being used (check analytics)

### Performance Monitoring
- [ ] Check slow query logs
- [ ] Monitor disk space usage
- [ ] Monitor database connection pool
- [ ] Verify indexes are being used

### User Communication
- [ ] Changelog/release notes published
- [ ] Users notified of new capabilities
- [ ] Support team trained on new features
- [ ] FAQ updated with common questions

### Long-term Maintenance
- [ ] Schedule Crawl4AI library updates
- [ ] Monitor ReportLab for security updates
- [ ] Review and optimize PDF generation if needed
- [ ] Analyze scraping failure patterns
- [ ] Plan performance improvements

---

## Troubleshooting During Deployment

### Issue: "Module 'crawl4ai' not found"
**Solution:**
```bash
pip install crawl4ai==0.4.243
python -c "import crawl4ai; print(crawl4ai.__version__)"
```

### Issue: "Relation 'documents' does not exist"
**Solution:**
```bash
psql -d securerag -f migrations/add_documents_table.sql
psql -d securerag -c "SELECT * FROM documents LIMIT 1;"
```

### Issue: "Cloudinary upload failing"
**Solution:**
```bash
# Verify credentials
grep CLOUDINARY .env
curl -X POST http://localhost:8000/api/v1/documents/upload -F files=@test.pdf
# Check Cloudinary console for errors
```

### Issue: "Web scraping timeout"
**Solution:**
```bash
# Increase timeout in .env
CRAWL4AI_TIMEOUT=60
# Restart service
sudo systemctl restart securerag-backend
```

### Issue: "Database migration failed"
**Solution:**
```bash
# Rollback and check syntax
psql -d securerag -c "DROP TABLE IF EXISTS documents;"
cat migrations/add_documents_table.sql | psql -d securerag
# Verify created
psql -d securerag -c "\dt documents"
```

---

## Sign-Off

Once all checks pass, add sign-off:

- [ ] Tested by: _________________ (name)
- [ ] Deployed by: _________________ (name)
- [ ] Verified by: _________________ (name)
- [ ] Date: _________________ (YYYY-MM-DD)

---

**Last Updated**: 2026-06-26  
**Status**: Ready for deployment  
**Critical Path**: Install deps → Migrate DB → Set config → Restart → Test
