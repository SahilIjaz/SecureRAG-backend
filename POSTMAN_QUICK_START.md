# Postman Quick Start (5 Minutes)

## Step 1: Open Postman & Set Base URL (1 min)

1. Open **Postman**
2. Create **New Request**
3. Set Method: **POST**
4. Set URL: `http://localhost:8000/api/v1/auth/signin`

---

## Step 2: Sign In (1 min)

### Headers
```
Content-Type: application/json
```

### Body (raw JSON)
```json
{
  "email": "your@example.com",
  "password": "yourpassword"
}
```

### Click Send

**Response** (you'll get something like):
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "...",
  "token_type": "bearer"
}
```

**Copy the `access_token` value** ✂️

---

## Step 3: Set Up Environment Variables (1 min)

1. Top right → Click **Environments** dropdown
2. Click **Create** → Name: `Local`
3. Add two variables:

| Variable | Value |
|----------|-------|
| `base_url` | `http://localhost:8000` |
| `token` | (paste your access_token here) |

4. Click **Save**

---

## Step 4: Upload PDF (1 min)

### Create New Request

1. New Request
2. Method: **POST**
3. URL: `{{base_url}}/api/v1/documents/upload`

### Headers
```
Authorization: Bearer {{token}}
```
(Postman will auto-set Content-Type for form-data)

### Body
1. Click **Body** tab
2. Select **form-data**
3. Key: `files` | Type: **File** | Value: (select your PDF)
4. Click **Send**

**Success Response** (201):
```json
{
  "message": "1 document(s) uploaded successfully.",
  "documents": [{
    "id": "...",
    "original_filename": "your_file.pdf",
    "file_size_mb": 2.5,
    "source": "uploaded",
    "file_url": "https://res.cloudinary.com/..."
  }],
  "total_count": 1,
  "total_storage_mb": 2.5
}
```

✅ **Success!** Your PDF is now on Cloudinary.

---

## Step 5: Scrape Website (1 min)

### Create New Request

1. New Request
2. Method: **POST**
3. URL: `{{base_url}}/api/v1/documents/scrape`

### Headers
```
Authorization: Bearer {{token}}
Content-Type: application/json
```

### Body (raw JSON)
```json
{
  "urls": [
    "https://github.com/unclecode/crawl4ai"
  ]
}
```

### Click Send

**Success Response** (201):
```json
{
  "message": "1 website(s) scraped and added successfully.",
  "documents": [{
    "id": "...",
    "original_filename": "Crawl4AI",
    "file_size_mb": 1.2,
    "source": "scraped",
    "source_url": "https://github.com/unclecode/crawl4ai",
    "file_url": "https://res.cloudinary.com/..."
  }],
  "total_count": 1,
  "total_storage_mb": 1.2
}
```

✅ **Success!** Website scraped and converted to PDF.

---

## Bonus: List All Documents

### Create New Request

1. New Request
2. Method: **GET**
3. URL: `{{base_url}}/api/v1/documents/`

### Headers
```
Authorization: Bearer {{token}}
```

### Click Send

See all your documents (uploaded + scraped):
```json
{
  "message": "Documents retrieved successfully.",
  "documents": [
    {
      "original_filename": "your_file.pdf",
      "source": "uploaded",
      "source_url": null
    },
    {
      "original_filename": "Crawl4AI",
      "source": "scraped",
      "source_url": "https://github.com/unclecode/crawl4ai"
    }
  ],
  "total_count": 2,
  "total_storage_mb": 3.7
}
```

---

## 🎯 That's It!

You just:
✅ Signed in  
✅ Uploaded a PDF  
✅ Scraped a website  
✅ Listed all documents  

---

## 📋 Quick Reference

### Upload Multiple PDFs
```
Body (form-data):
  files: document1.pdf
  files: document2.pdf
  files: document3.pdf
```

### Scrape Multiple Websites
```json
{
  "urls": [
    "https://github.com",
    "https://example.com",
    "https://wikipedia.org"
  ]
}
```

### Test Error Cases

**File too large (> 50MB)**
- Response: 413
- Message: "exceeds the 50MB per-file limit"

**Invalid URL (no http://)**
- Response: 400
- Message: "Must start with http:// or https://"

**Quota exceeded (10 docs for Free plan)**
- Response: 403
- Message: "Document quota exceeded"

---

## 💡 Pro Tips

### Auto-save Token
After sign in, right-click `access_token` in response → Copy as Environment Variable

### Keyboard Shortcut
Press `Cmd+Enter` (Mac) or `Ctrl+Enter` (Windows) to send request

### Pretty Print Response
Response → Click **Pretty** to format JSON nicely

### Copy Response
Click the copy icon to copy entire response

---

## 🔗 Full Guides

For more detailed testing:
- See **POSTMAN_TESTING_GUIDE.md** (comprehensive)
- See **QUICK_START_WEB_SCRAPING.md** (general setup)

---

Done! 🎉 You're ready to test!
