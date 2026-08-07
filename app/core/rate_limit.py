"""
Shared slowapi rate limiter for the frontend-compat API surface.

Previously main.py, and every app/api/frontend/*.py router, each
constructed their own separate `Limiter(key_func=get_remote_address)`
instance — only the one in main.py was ever wired to `app.state.limiter`
(what the RateLimitExceeded exception handler and slowapi's internal
storage lookups actually key off), so the others were redundant instances
of the same in-memory counter store. Consolidating to one shared instance
here is a correctness fix independent of the key_func change below.

Keying by tenant (instead of raw IP) means users behind the same NAT/proxy
don't share a rate-limit bucket, and an attacker can't trivially evade a
limit by rotating source IPs while reusing the same account. Unauthenticated
routes (signup, login, forgot-password) have no tenant yet, so they still
fall back to IP.
"""

from jose import JWTError, jwt
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.config import settings

def _tenant_or_ip_key(request: Request) -> str:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        except JWTError:
            payload = None
        tenant_id = payload.get("tenant_id") if payload else None
        if tenant_id:
            return f"tenant:{tenant_id}"
    return get_remote_address(request)

limiter = Limiter(key_func=_tenant_or_ip_key)
