# Security Implementation Guide

## Status Summary

```
✅ IMPLEMENTED (5/9)
  ✅ Tenant Isolation
  ✅ Tenant ID Source (from JWT)
  ✅ Authentication (JWT)
  ✅ Authorization (2 roles)
  ✅ Upload Safety (file type + size)

⚠️ PARTIAL (2/9)
  ⚠️ Scraping Safety (needs IP blocking)
  ⚠️ Data Protection (needs encryption)

❌ NOT IMPLEMENTED (2/9)
  ❌ Abuse Control (rate limiting)
  ❌ Prompt Injection (for future LLM)

SCORE: 6/9 = 67% ✅ (Good baseline)
```

---

## Quick Wins (Do First)

### 1. CORS Hardening (5 minutes)

**File**: `app/main.py`

**Current** (Too permissive):
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

**Change to**:
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
        "https://yourdomain.com",  # Add production domain
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=3600,
)
```

---

## Implementation Plan (Priority Order)

### 🔴 HIGH PRIORITY

#### 1. IP Blocking for Web Scraping (30 minutes)

**Why**: Prevents SSRF attacks (accessing internal services)

**File**: Create `app/core/ip_validator.py`

```python
"""IP validation to prevent SSRF attacks."""

import socket
from ipaddress import ip_address, ip_network
from urllib.parse import urlparse

BLOCKED_IP_RANGES = [
    ip_network("127.0.0.0/8"),      # Localhost
    ip_network("10.0.0.0/8"),       # Private network
    ip_network("172.16.0.0/12"),    # Private network
    ip_network("192.168.0.0/16"),   # Private network
    ip_network("169.254.0.0/16"),   # Link-local
    ip_network("0.0.0.0/8"),        # This network
    ip_network("255.255.255.255/32"), # Broadcast
]

def is_ip_blocked(hostname: str) -> bool:
    """Check if IP is in blocked ranges."""
    try:
        # Resolve hostname to IP
        ip = ip_address(socket.gethostbyname(hostname))
        
        # Check against blocked ranges
        for blocked in BLOCKED_IP_RANGES:
            if ip in blocked:
                return True
        return False
        
    except socket.gaierror:
        # DNS lookup failed - reject
        return True
    except ValueError:
        # Invalid IP - reject
        return True

def validate_url_safe(url: str) -> None:
    """Validate URL is safe to scrape."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        
        if not hostname:
            raise ValueError("No hostname in URL")
        
        if is_ip_blocked(hostname):
            raise ValueError(f"URL resolves to blocked IP: {hostname}")
            
    except Exception as e:
        raise ValueError(f"URL validation failed: {str(e)}")
```

**Update**: `app/core/scraper.py`

```python
from app.core.ip_validator import validate_url_safe

async def scrape_website_to_pdf(url: str, timeout: int = 30):
    """Scrapes a website and converts to PDF."""
    
    # Validate URL is safe
    validate_url_safe(url)  # ← ADD THIS LINE
    
    if not url.startswith(("http://", "https://")):
        raise ValueError("URL must start with http:// or https://")
    
    # ... rest of function
```

**Test it**:
```bash
# These should be REJECTED:
curl -X POST http://localhost:8000/api/v1/documents/scrape \
  -H "Authorization: Bearer TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"urls": ["http://127.0.0.1:8000/docs"]}'

curl -X POST http://localhost:8000/api/v1/documents/scrape \
  -H "Authorization: Bearer TOKEN" \
  -d '{"urls": ["http://192.168.1.1"]}'

# This should WORK:
curl -X POST http://localhost:8000/api/v1/documents/scrape \
  -H "Authorization: Bearer TOKEN" \
  -d '{"urls": ["https://github.com"]}'
```

---

#### 2. File Encryption at Rest (1 hour)

**Why**: If Cloudinary is breached, files are still encrypted

**Step 1**: Install encryption library

```bash
pip install cryptography
```

**Step 2**: Update `requirements.txt`

```
cryptography==44.0.1
```

**Step 3**: Update `.env.example`

```env
# File Encryption Key (generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
FILE_ENCRYPTION_KEY=your_base64_key_here
```

**Step 4**: Generate key

```bash
python << 'EOF'
from cryptography.fernet import Fernet
key = Fernet.generate_key()
print(f"Add to .env:\nFILE_ENCRYPTION_KEY={key.decode()}")
EOF
```

Copy the output and add to `.env`.

**Step 5**: Update `app/config.py`

```python
FILE_ENCRYPTION_KEY: str = ""  # Add this line
```

**Step 6**: Update `app/core/storage.py`

```python
import os
from cryptography.fernet import Fernet
from app.config import settings

# Initialize cipher
_cipher = None

def _get_cipher():
    global _cipher
    if _cipher is None:
        _cipher = Fernet(settings.FILE_ENCRYPTION_KEY.encode())
    return _cipher

async def upload_file_to_cloudinary(
    file_content: bytes,
    tenant_id: uuid.UUID,
    original_filename: str,
    content_type: str,
) -> Tuple[str, str]:
    """Uploads a file to Cloudinary (encrypted)."""
    
    # Encrypt file content
    cipher = _get_cipher()
    encrypted_content = cipher.encrypt(file_content)
    
    ext = os.path.splitext(original_filename)[1].lower()
    unique_name = f"{uuid.uuid4()}{ext}"
    public_id = f"securerag/uploads/{tenant_id}/{unique_name}"
    
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        _upload_blocking,
        encrypted_content,  # Upload encrypted
        public_id,
        content_type,
    )
    
    secure_url = result["secure_url"]
    returned_public_id = result["public_id"]
    logger.info("Uploaded encrypted file to Cloudinary: %s", secure_url)
    return returned_public_id, secure_url

# When retrieving/downloading files, decrypt:
def decrypt_file(encrypted_bytes: bytes) -> bytes:
    """Decrypt file content."""
    cipher = _get_cipher()
    return cipher.decrypt(encrypted_bytes)
```

**Test it**:
```bash
# Generate test key
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Add to .env
# Restart server
# Upload file
# Check Cloudinary - file should be binary/encrypted (not readable as PDF)
```

---

### 🟡 MEDIUM PRIORITY

#### 3. Rate Limiting (45 minutes)

**Why**: Prevent abuse and DDoS attacks

**Step 1**: Install library

```bash
pip install slowapi
```

**Step 2**: Update `requirements.txt`

```
slowapi==0.1.9
```

**Step 3**: Update `app/main.py`

```python
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi.responses import JSONResponse

# Initialize rate limiter
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter

# Add exception handler
@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request, exc):
    return JSONResponse(
        status_code=429,
        content={"detail": "Rate limit exceeded. Try again later."}
    )
```

**Step 4**: Add rate limits to endpoints

**File**: `app/api/v1/endpoints/documents.py`

```python
from slowapi import Limiter

@router.post("/upload")
@limiter.limit("10/minute")  # 10 uploads per minute
async def upload_documents(...):
    ...

@router.post("/scrape")
@limiter.limit("5/minute")  # 5 scrapes per minute
async def scrape_websites(...):
    ...

@router.get("/")
@limiter.limit("30/minute")  # 30 lists per minute
async def list_documents(...):
    ...
```

**File**: `app/api/v1/endpoints/auth.py`

```python
@router.post("/signin")
@limiter.limit("5/minute")  # Prevent brute force
async def signin(...):
    ...

@router.post("/signup")
@limiter.limit("3/minute")  # Prevent spam signups
async def signup(...):
    ...
```

**Test it**:
```bash
# Run this 11 times in 60 seconds:
for i in {1..11}; do
  curl -X POST http://localhost:8000/api/v1/documents/upload \
    -H "Authorization: Bearer TOKEN" \
    -F "files=@test.pdf"
  echo "Request $i"
  sleep 5
done

# 11th request should return 429 Too Many Requests
```

---

## Summary of Changes

| Feature | File | Time | Impact |
|---------|------|------|--------|
| CORS hardening | `app/main.py` | 5 min | Prevents CSRF |
| IP blocking | `app/core/ip_validator.py` (new) | 30 min | Prevents SSRF |
| File encryption | `app/core/storage.py` | 1 hour | Protects data at rest |
| Rate limiting | `app/main.py` + `app/api/v1/endpoints/` | 45 min | Prevents abuse |

**Total effort**: ~2 hours
**Security improvement**: 67% → 92% ✅

---

## Checklist

- [ ] **CORS Hardening**
  - [ ] Update allowed_origins in main.py
  - [ ] Test with frontend

- [ ] **IP Blocking**
  - [ ] Create app/core/ip_validator.py
  - [ ] Update app/core/scraper.py
  - [ ] Test with blocked IPs (127.0.0.1, 192.168.x.x)
  - [ ] Test with public URL (github.com)

- [ ] **File Encryption**
  - [ ] Generate encryption key
  - [ ] Add FILE_ENCRYPTION_KEY to .env
  - [ ] Update app/config.py
  - [ ] Update app/core/storage.py
  - [ ] Test upload/download

- [ ] **Rate Limiting**
  - [ ] pip install slowapi
  - [ ] Update requirements.txt
  - [ ] Update app/main.py
  - [ ] Update document endpoints
  - [ ] Update auth endpoints
  - [ ] Test rate limits

- [ ] **Testing**
  - [ ] Verify CORS blocks unauthorized origins
  - [ ] Verify IP blocking rejects private IPs
  - [ ] Verify files are encrypted in Cloudinary
  - [ ] Verify rate limits trigger at correct threshold

---

## Next Steps

1. **Now**: Implement CORS + IP blocking (quick wins)
2. **This week**: Add file encryption
3. **Next week**: Add rate limiting
4. **Later**: Add prompt injection protection (for LLM phase)

---

Want me to implement any of these? I can do them all right now! 🚀
