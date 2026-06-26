# Postman Testing Guide: PDF Upload & Web Scraping

## Overview

This guide shows you how to test all document upload and web scraping endpoints using Postman.

---

## Prerequisites

1. **Postman** installed (download from https://www.postman.com/downloads/)
2. **Running server** at `http://localhost:8000`
3. **Valid authentication token** from sign in or signup
4. **PDF test file** (create or use existing)

---

## Step 1: Get Authentication Token

### Option A: Sign In (if you have existing account)

**Endpoint**: `POST` `http://localhost:8000/api/v1/auth/signin`

**Headers**:
```
Content-Type: application/json
```

**Body** (raw JSON):
```json
{
  "email": "your@example.com",
  "password": "yourpassword"
}
```

**Expected Response** (201):
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer"
}
```

**Copy the `access_token` value** - you'll use this for all document endpoints.

---

### Option B: Sign Up (if you need new account)

**Endpoint**: `POST` `http://localhost:8000/api/v1/auth/signup`

**Headers**:
```
Content-Type: application/json
```

**Body**:
```json
{
  "full_name": "Test User",
  "email": "test@example.com",
  "password": "TestPassword123"
}
```

Then follow Steps 2-5 of onboarding to complete signup and get access token.

---

## Step 2: Set Up Postman Collection

### Create Collection

1. Click **Collections** → **Create New Collection**
2. Name it: `SecureRAG Document API`
3. Click **Create**

### Add Environment Variable

1. Click **Environments** → **Create New Environment**
2. Name it: `SecureRAG Local`
3. Add variables:

| Variable | Initial Value | Type |
|----------|---------------|------|
| `base_url` | `http://localhost:8000` | string |
| `access_token` | `your_token_here` | string |

4. Click **Save**

### Select Environment

1. Top right dropdown - select **SecureRAG Local**

---

## Step 3: Test PDF Upload Endpoint

### Create Request

1. New request → Name: `Upload PDF Document`
2. Method: **POST**
3. URL: `{{base_url}}/api/v1/documents/upload`

### Setup Headers

Click **Headers** tab:

| Key | Value |
|-----|-------|
| `Authorization` | `Bearer {{access_token}}` |

(Leave Content-Type empty - Postman will set it automatically for form-data)

### Setup Body

Click **Body** tab → Select **form-data**

| Key | Value | Type |
|-----|-------|------|
| `files` | (select your PDF file) | File |

**How to select file**:
1. In the **Value** column, click the file icon
2. Browse and select a PDF file from your computer
3. (Optional) Add multiple files by clicking **+ Add** and repeating

### Send Request

Click **Send**

### Expected Response (201)

```json
{
  "message": "1 document(s) uploaded successfully.",
  "documents": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "original_filename": "your_file.pdf",
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
  "total_count": 1,
  "total_storage_mb": 2.5
}
```

### Test Edge Cases

#### Upload Multiple PDFs

**Body** (form-data):
```
files: file1.pdf (File)
files: file2.pdf (File)
files: file3.pdf (File)
```

Expected: All 3 should upload successfully

#### Upload Oversized File (> 50MB)

**Body** (form-data):
```
files: large_file.pdf (> 50MB)
```

Expected Response (413):
```json
{
  "detail": "'large_file.pdf' exceeds the 50MB per-file limit."
}
```

#### Upload Non-PDF File

**Body** (form-data):
```
files: document.docx (File)
```

Expected Response (415):
```json
{
  "detail": "File type 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' is not allowed. Allowed: PDF, DOCX, DOC, TXT."
}
```

---

## Step 4: Test Web Scraping Endpoint

### Create Request

1. New request → Name: `Scrape Website`
2. Method: **POST**
3. URL: `{{base_url}}/api/v1/documents/scrape`

### Setup Headers

Click **Headers** tab:

| Key | Value |
|-----|-------|
| `Authorization` | `Bearer {{access_token}}` |
| `Content-Type` | `application/json` |

### Setup Body

Click **Body** tab → Select **raw** → Choose **JSON**

```json
{
  "urls": [
    "https://github.com/unclecode/crawl4ai"
  ]
}
```

### Send Request

Click **Send**

### Expected Response (201)

```json
{
  "message": "1 website(s) scraped and added successfully.",
  "documents": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440001",
      "original_filename": "Crawl4AI - GitHub",
      "file_size_mb": 1.2,
      "mime_type": "application/pdf",
      "source": "scraped",
      "status": "pending",
      "sample_document_id": null,
      "source_url": "https://github.com/unclecode/crawl4ai",
      "file_url": "https://res.cloudinary.com/...",
      "created_at": "2026-06-26T12:00:00+00:00"
    }
  ],
  "total_count": 1,
  "total_storage_mb": 1.2
}
```

### Test Multiple URLs

**Body** (raw JSON):
```json
{
  "urls": [
    "https://github.com/unclecode/crawl4ai",
    "https://www.example.com",
    "https://www.wikipedia.org"
  ]
}
```

Expected: All 3 should scrape successfully

### Test Edge Cases

#### Invalid URL (missing protocol)

**Body**:
```json
{
  "urls": [
    "example.com"
  ]
}
```

Expected Response (400):
```json
{
  "detail": "Invalid URL: example.com. Must start with http:// or https://"
}
```

#### Unreachable URL

**Body**:
```json
{
  "urls": [
    "https://thisdomain-definitely-does-not-exist-12345.com"
  ]
}
```

Expected Response (400):
```json
{
  "detail": "Failed to scrape https://thisdomain-definitely-does-not-exist-12345.com: ..."
}
```

#### No URLs Provided

**Body**:
```json
{
  "urls": []
}
```

Expected Response (400):
```json
{
  "detail": "No URLs provided."
}
```

---

## Step 5: Test List Documents Endpoint

### Create Request

1. New request → Name: `List All Documents`
2. Method: **GET**
3. URL: `{{base_url}}/api/v1/documents/`

### Setup Headers

Click **Headers** tab:

| Key | Value |
|-----|-------|
| `Authorization` | `Bearer {{access_token}}` |

### Send Request

Click **Send**

### Expected Response (200)

```json
{
  "message": "Documents retrieved successfully.",
  "documents": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "original_filename": "your_file.pdf",
      "file_size_mb": 2.5,
      "mime_type": "application/pdf",
      "source": "uploaded",
      "status": "pending",
      "sample_document_id": null,
      "source_url": null,
      "file_url": "https://res.cloudinary.com/...",
      "created_at": "2026-06-26T12:00:00+00:00"
    },
    {
      "id": "550e8400-e29b-41d4-a716-446655440001",
      "original_filename": "Crawl4AI - GitHub",
      "file_size_mb": 1.2,
      "mime_type": "application/pdf",
      "source": "scraped",
      "status": "pending",
      "sample_document_id": null,
      "source_url": "https://github.com/unclecode/crawl4ai",
      "file_url": "https://res.cloudinary.com/...",
      "created_at": "2026-06-26T12:00:00+00:00"
    }
  ],
  "total_count": 2,
  "total_storage_mb": 3.7
}
```

**Notice**:
- Uploaded documents have `source: "uploaded"` and `source_url: null`
- Scraped documents have `source: "scraped"` and `source_url: "..."`

---

## Complete Postman Collection (JSON)

You can import this directly into Postman.

Save as `SecureRAG-Documents.postman_collection.json`:

```json
{
  "info": {
    "name": "SecureRAG Document API",
    "description": "PDF Upload and Web Scraping API",
    "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
  },
  "item": [
    {
      "name": "Sign In",
      "request": {
        "method": "POST",
        "header": [
          {
            "key": "Content-Type",
            "value": "application/json"
          }
        ],
        "body": {
          "mode": "raw",
          "raw": "{\n  \"email\": \"your@example.com\",\n  \"password\": \"yourpassword\"\n}"
        },
        "url": {
          "raw": "{{base_url}}/api/v1/auth/signin",
          "host": ["{{base_url}}"],
          "path": ["api", "v1", "auth", "signin"]
        }
      }
    },
    {
      "name": "Upload PDF Document",
      "request": {
        "method": "POST",
        "header": [
          {
            "key": "Authorization",
            "value": "Bearer {{access_token}}"
          }
        ],
        "body": {
          "mode": "formdata",
          "formdata": [
            {
              "key": "files",
              "type": "file",
              "src": []
            }
          ]
        },
        "url": {
          "raw": "{{base_url}}/api/v1/documents/upload",
          "host": ["{{base_url}}"],
          "path": ["api", "v1", "documents", "upload"]
        }
      }
    },
    {
      "name": "Scrape Website",
      "request": {
        "method": "POST",
        "header": [
          {
            "key": "Authorization",
            "value": "Bearer {{access_token}}"
          },
          {
            "key": "Content-Type",
            "value": "application/json"
          }
        ],
        "body": {
          "mode": "raw",
          "raw": "{\n  \"urls\": [\n    \"https://github.com/unclecode/crawl4ai\"\n  ]\n}"
        },
        "url": {
          "raw": "{{base_url}}/api/v1/documents/scrape",
          "host": ["{{base_url}}"],
          "path": ["api", "v1", "documents", "scrape"]
        }
      }
    },
    {
      "name": "List All Documents",
      "request": {
        "method": "GET",
        "header": [
          {
            "key": "Authorization",
            "value": "Bearer {{access_token}}"
          }
        ],
        "url": {
          "raw": "{{base_url}}/api/v1/documents/",
          "host": ["{{base_url}}"],
          "path": ["api", "v1", "documents", ""]
        }
      }
    }
  ],
  "variable": [
    {
      "key": "base_url",
      "value": "http://localhost:8000",
      "type": "string"
    },
    {
      "key": "access_token",
      "value": "",
      "type": "string"
    }
  ]
}
```

### Import into Postman

1. Click **Import** (top left)
2. Paste the JSON above or upload the file
3. Collection appears in sidebar
4. Set `access_token` in environment variables

---

## Testing Checklist

### Upload Functionality
- [ ] Upload single PDF (< 50MB) → Success
- [ ] Upload multiple PDFs → All success
- [ ] Upload oversized PDF (> 50MB) → 413 error
- [ ] Upload DOCX file → 415 error
- [ ] Upload with invalid token → 401 error

### Scraping Functionality
- [ ] Scrape valid website → Success
- [ ] Scrape multiple URLs → All success
- [ ] Scrape invalid URL (no protocol) → 400 error
- [ ] Scrape unreachable URL → 400 error
- [ ] Scrape with empty URL list → 400 error
- [ ] Scrape with invalid token → 401 error

### Document Management
- [ ] List documents shows both uploads and scrapes
- [ ] Uploaded docs have `source: "uploaded"`
- [ ] Scraped docs have `source: "scraped"`
- [ ] Scraped docs have `source_url` set
- [ ] Uploaded docs have `source_url: null`
- [ ] All docs have `file_url` (Cloudinary link)

### Error Handling
- [ ] Invalid Authorization header → 401
- [ ] Missing Authorization header → 401
- [ ] Expired token → 401
- [ ] Malformed request body → 422
- [ ] Wrong content type → 415 or 422

---

## Example Test Scenarios

### Scenario 1: Complete Upload Flow

1. **Sign In** → Get token
2. **Update environment** → Paste token
3. **Upload PDF** → See file in Cloudinary
4. **List Documents** → See new document with `source: "uploaded"`

### Scenario 2: Complete Scraping Flow

1. **Sign In** → Get token
2. **Update environment** → Paste token
3. **Scrape GitHub** → See success response
4. **Wait** → Scraping might take 10-30 seconds
5. **List Documents** → See new document with `source: "scraped"` and `source_url`

### Scenario 3: Quota Testing

1. **Upload** → 1st document (OK)
2. **Upload** → 2nd document (OK)
3. **Upload** → 3rd document (OK)
4. **Upload** → 4th document (OK)
5. **Upload** → 5th document (OK)
6. **Upload** → 6th document (OK)
7. **Upload** → 7th document (OK)
8. **Upload** → 8th document (OK)
9. **Upload** → 9th document (OK)
10. **Upload** → 10th document (OK) ← Limit reached for Free plan
11. **Upload** → 11th document → **403 Forbidden** (quota exceeded)

---

## Response Status Codes

| Code | Meaning | Example |
|------|---------|---------|
| 201 | Created | Document uploaded/scraped successfully |
| 200 | OK | Documents listed successfully |
| 400 | Bad Request | Invalid URL, missing files, etc. |
| 401 | Unauthorized | Invalid or missing token |
| 403 | Forbidden | Quota exceeded |
| 413 | Payload Too Large | File > max_file_size_mb |
| 415 | Unsupported Media Type | File type not PDF |
| 422 | Unprocessable Entity | Validation error |

---

## Tips & Tricks

### Save Token in Environment Variable

After sign in response:

1. Click the response body
2. Find `access_token` value
3. Copy the value
4. Click **Environments** → **SecureRAG Local**
5. Paste into `access_token` variable
6. Click **Save**

Or use Postman's **Tests** feature to auto-save:

```javascript
var jsonData = pm.response.json();
pm.environment.set("access_token", jsonData.access_token);
```

### Pre-request Scripts

Generate timestamp for dynamic filenames:

```javascript
pm.environment.set("timestamp", new Date().getTime());
```

### Create Test PDF Locally

Using Python:

```python
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

c = canvas.Canvas("test.pdf", pagesize=letter)
c.drawString(100, 750, "Test PDF Document")
c.drawString(100, 730, "Created for SecureRAG testing")
c.save()
```

Or use any online PDF generator:
- https://www.ilovepdf.com/
- https://smallpdf.com/

---

## Common Issues & Solutions

### Issue: 401 Unauthorized

**Problem**: Token is missing or expired

**Solution**:
1. Sign in again to get new token
2. Copy `access_token` from response
3. Update `{{access_token}}` in environment

### Issue: 413 Request Entity Too Large

**Problem**: File is > 50MB

**Solution**:
1. Use smaller PDF file
2. Or upgrade subscription plan

### Issue: Timeout waiting for response

**Problem**: Scraping is taking too long

**Solution**:
1. Increase Postman timeout: Settings → General → Request timeout
2. Or try scraping a simpler website
3. Check `CRAWL4AI_TIMEOUT` in server config (should be 30s)

### Issue: Cloudinary URL returns 404

**Problem**: File not found on Cloudinary

**Solution**:
1. Verify Cloudinary credentials in `.env`
2. Check Cloudinary account has available storage
3. Restart server and try again

---

## Advanced Testing

### Batch Upload Test

Create multiple requests in sequence:

1. Create folder: `Batch Upload`
2. Add 5 requests with different PDF files
3. Click **Run** on folder
4. Watch all requests execute in sequence

### Performance Test

1. Upload same file 10 times
2. Note response times
3. Check if performance degrades

### Concurrent Requests

1. Open 3 Postman tabs
2. Each with different URL to scrape
3. Click Send on all 3 simultaneously
4. Monitor if system handles concurrent requests

---

## Swagger UI Alternative

Instead of Postman, you can also test in Swagger UI:

1. Open browser: `http://localhost:8000/docs`
2. Click **Authorize** button
3. Enter your bearer token
4. Try endpoints interactively

---

**Last Updated**: 2026-06-26  
**Status**: Complete  
**Tested With**: Postman v11.0+
