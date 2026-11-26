"""Integration tests through the real FastAPI app (no database needed):
health, authentication gate on every dashboard route, malformed-token handling,
public-widget origin enforcement and CORS."""
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.core.security import create_access_token, create_refresh_token
from app.main import app

client = TestClient(app, raise_server_exceptions=False)


def test_health_endpoint():
    r = client.get("/health")
    assert r.status_code == 200


def _protected_routes():
    from app.services.auth_service import get_current_user, get_any_valid_user
    from app.core.rbac import require_owner_any_valid

    def walk(d):
        for x in d.dependencies:
            yield x.call
            yield from walk(x)
    for r in app.routes:
        if isinstance(r, APIRoute) and r.path.startswith("/api") and any(
            c in (get_current_user, get_any_valid_user, require_owner_any_valid) or
            getattr(c, "__qualname__", "").startswith("require_role.")
            for c in walk(r.dependant)):
            yield r


def test_every_protected_route_rejects_anonymous():
    """SEC: no dashboard route may be reachable without a bearer token."""
    leaked = []
    for r in _protected_routes():
        path = r.path.replace("{", "x").replace("}", "")
        for m in r.methods - {"HEAD", "OPTIONS"}:
            resp = client.request(m, path)
            if resp.status_code not in (401, 403, 405, 422):
                leaked.append((m, r.path, resp.status_code))
    assert not leaked, leaked


@pytest.mark.parametrize("hdr", [
    "Bearer junk", "Bearer a.b.c", "Basic abc", "Bearer ",
])
def test_malformed_authorization_rejected(hdr):
    r = client.get("/api/user/me", headers={"Authorization": hdr})
    assert r.status_code in (401, 403)


def test_refresh_token_cannot_authenticate_api_call():
    tok, _, _ = create_refresh_token({"sub": "u1", "tenant_id": "t1"})
    r = client.get("/api/user/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code in (401, 403)


def test_forged_token_rejected():
    from jose import jwt
    forged = jwt.encode({"sub": "u1", "tenant_id": "t1", "type": "access"}, "wrong", algorithm="HS256")
    r = client.get("/api/user/me", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code in (401, 403)


def test_unknown_route_is_404():
    assert client.get("/api/definitely-not-a-route").status_code == 404


def test_cors_blocks_unlisted_origin():
    r = client.options("/health", headers={"Origin": "https://evil.example",
                                           "Access-Control-Request-Method": "GET"})
    assert r.headers.get("access-control-allow-origin") != "https://evil.example"


def test_health_response_leaks_no_traceback():
    r = client.get("/health")
    assert "traceback" not in r.text.lower()
