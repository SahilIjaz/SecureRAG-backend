"""
Security helpers for the public chat widget (app/api/public/widget.py):
origin allowlist checking and the signed visitor-session token.

Deliberately separate from app/core/security.py (dashboard login JWTs) —
the two token types must never be interchangeable.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

from jose import JWTError, jwt

from app.config import settings

WIDGET_SESSION_TOKEN_TYPE = "widget_session"
WIDGET_SESSION_TOKEN_TTL_HOURS = 24

def _hostname(origin_or_referer: str) -> Optional[str]:
    host = urlparse(origin_or_referer).hostname
    return host.lower() if host else None

def _normalize_allowlist_entry(entry: str) -> Optional[str]:
    """
    Reduces one allowedDomains entry down to a bare hostname, the same shape
    _hostname() produces from a real Origin header — so "acme.com",
    "https://acme.com", and "http://localhost:5173" all normalize to the
    same comparable value regardless of how the tenant typed it in.
    """
    entry = entry.strip()
    if not entry:
        return None
    if "://" in entry:
        return _hostname(entry)
    # No scheme: entry is already meant to be a bare hostname, but may still
    # carry a stray ":port" or "/path" if someone pasted more than a domain.
    host = entry.split("/", 1)[0].split(":", 1)[0].strip().lower()
    return host or None

def is_origin_allowed(origin_header: Optional[str], allowed_domains_csv: str) -> bool:
    """
    Exact-hostname allowlist check — never substring/suffix matching, so an
    allowlist entry of "example.com" can't be satisfied by
    "evil-example.com" or "example.com.evil.net".

    A browser can't lie about its own Origin header on a cross-origin
    fetch/XHR, which is what makes this check meaningful. A missing Origin
    header means the caller isn't a browser (curl, server-side Python, etc.)
    — those are allowed through here (the key + rate limiting + quota are
    what bound a direct API caller, not the domain check; the key is already
    visible in the widget's page source to begin with).
    """
    if not origin_header:
        return True
    host = _hostname(origin_header)
    if not host:
        return False
    allowed = {
        normalized
        for d in allowed_domains_csv.split(",")
        if (normalized := _normalize_allowlist_entry(d))
    }
    return host in allowed

def create_widget_session_token(tenant_id: str, conversation_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=WIDGET_SESSION_TOKEN_TTL_HOURS)
    payload = {
        "tenant_id": tenant_id,
        "conversation_id": conversation_id,
        "type": WIDGET_SESSION_TOKEN_TYPE,
        "exp": expire,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

def decode_widget_session_token(token: str) -> Optional[dict]:
    """Returns the payload if valid, or None for any failure (expired, tampered,
    wrong type, or not a widget session token at all) — callers should treat
    None as "no session" and start fresh rather than erroring the request."""
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        return None
    if payload.get("type") != WIDGET_SESSION_TOKEN_TYPE:
        return None
    if not payload.get("tenant_id") or not payload.get("conversation_id"):
        return None
    return payload
