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

def _trusted_frontend_hosts() -> set[str]:
    """Hostnames the widget *page itself* is served from (the Nexus frontend
    and local dev ports) — requests from these are the embed iframe talking,
    not a customer site, so the embedding site must be read from the
    X-Widget-Host header instead of the Origin."""
    hosts = set()
    for origin in settings.cors_allowed_origins:
        host = _hostname(origin)
        if host:
            hosts.add(host)
    return hosts

def resolve_embed_host(origin_header: Optional[str], widget_host_header: Optional[str]) -> Optional[str]:
    """
    Which hostname should be checked against the tenant's allowed domains.

    The widget is embedded as an <iframe src="{NEXUS}/widget/…"> (see
    Frontend/public/widget-loader.js), so every API call it makes carries
    the *Nexus frontend's* Origin — the customer's domain never appears in
    any browser-controlled header. Before this, "Allowed domains = acme.com"
    could therefore never match and every real embed got 403 (silently —
    the widget renders nothing on a load error).

    Resolution:
      - Origin is one of our own frontend hosts → this is the iframe. The
        page reads the embedding site from `document.referrer` (set by the
        browser, not forgeable by the embedding page) and sends it as
        X-Widget-Host; that is the host to check. Missing header → None
        (rejected by the caller) rather than falling back to our own host.
      - Any other Origin → a direct browser call from a customer page (the
        "developer API" path); check the Origin's host as before.
      - No Origin at all → None (non-browser caller; see is_origin_allowed's
        WIDGET_REQUIRE_ORIGIN escape hatch).
    """
    if not origin_header:
        return None
    origin_host = _hostname(origin_header)
    if not origin_host:
        return None
    if origin_host in _trusted_frontend_hosts():
        return _normalize_allowlist_entry(widget_host_header or "") if widget_host_header else None
    return origin_host

def is_embed_allowed(
    origin_header: Optional[str], widget_host_header: Optional[str], allowed_domains_csv: str
) -> bool:
    """Exact-hostname allowlist check against the host resolve_embed_host()
    picks. Same escape hatch as is_origin_allowed for origin-less callers."""
    if not origin_header:
        return not settings.WIDGET_REQUIRE_ORIGIN
    host = resolve_embed_host(origin_header, widget_host_header)
    if not host:
        return False
    allowed = {
        normalized
        for d in allowed_domains_csv.split(",")
        if (normalized := _normalize_allowlist_entry(d))
    }
    return host in allowed

def is_origin_allowed(origin_header: Optional[str], allowed_domains_csv: str) -> bool:
    """
    Exact-hostname allowlist check — never substring/suffix matching, so an
    allowlist entry of "example.com" can't be satisfied by
    "evil-example.com" or "example.com.evil.net".

    A browser can't lie about its own Origin header on a cross-origin
    fetch/XHR, which is what makes this check meaningful. The widget always
    calls the API cross-origin (iframe on the site's origin → API host), so a
    real embed always carries an Origin.

    A MISSING Origin therefore means a non-browser caller (curl / server-side
    script). Previously those were allowed through, which let anyone who
    scraped the public widget key from a page hammer the message endpoint from
    a script and burn the tenant's monthly quota + LLM budget. When
    WIDGET_REQUIRE_ORIGIN is set (the default) we now reject them; the flag
    exists as an escape hatch if a legitimate non-browser integration needs
    the old behaviour.
    """
    if not origin_header:
        return not settings.WIDGET_REQUIRE_ORIGIN
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
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    # Hybrid post-quantum hardening: append an ML-DSA-44 signature over the
    # HMAC-signed JWT (see app/core/pq_crypto.py). Verification requires BOTH
    # layers to pass, so a quantum adversary who could forge one still fails.
    from app.core import pq_crypto

    if pq_crypto.token_signing_ready():
        token = pq_crypto.sign_token(token)
    return token

def decode_widget_session_token(token: str) -> Optional[dict]:
    """Returns the payload if valid, or None for any failure (expired, tampered,
    wrong type, or not a widget session token at all) — callers should treat
    None as "no session" and start fresh rather than erroring the request."""
    if not token:
        return None
    # Strip + verify the optional PQ signature layer first. A PQ-signed token
    # with a bad signature is rejected outright; a legacy (unsigned) token
    # passes through so sessions issued before the flag flip stay valid.
    from app.core import pq_crypto

    inner = pq_crypto.split_and_verify_token(token)
    if inner is None:
        return None
    try:
        payload = jwt.decode(inner, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        return None
    if payload.get("type") != WIDGET_SESSION_TOKEN_TYPE:
        return None
    if not payload.get("tenant_id") or not payload.get("conversation_id"):
        return None
    return payload
