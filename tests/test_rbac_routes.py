"""
RBAC regression tests — fail if a dashboard endpoint is left reachable by every
logged-in member (including agents) without an explicit decision.

Every route under /api that depends only on get_current_user must be listed in
AGENT_OK below; everything else must carry a role gate (require_owner /
require_admin / require_owner_any_valid) and the sensitive ones are asserted to
admit exactly the roles intended.
"""

from fastapi.routing import APIRoute

from app.core import pq_crypto
from app.core.rbac import require_owner_any_valid
from app.main import app
from app.models.tenant_user import TenantRole
from app.services.auth_service import get_any_valid_user, get_current_user

# (METHOD, path) pairs that ANY authenticated member (incl. agents) may call.
AGENT_OK = {
    # own account
    ("POST", "/api/auth/logout"), ("GET", "/api/user/me"), ("PUT", "/api/user/password"),
    ("GET", "/api/user/profile"), ("PUT", "/api/user/profile"), ("POST", "/api/user/avatar"),
    # inbox
    ("GET", "/api/conversations"), ("GET", "/api/conversations/live-summary"),
    ("GET", "/api/conversations/{conversation_id}"),
    ("PUT", "/api/conversations/{conversation_id}/status"),
    ("POST", "/api/conversations/{conversation_id}/reply"),
    ("POST", "/api/conversations/{conversation_id}/resolve"),
    ("POST", "/api/conversations/{conversation_id}/claim"),
    ("POST", "/api/conversations/{conversation_id}/join"),
    ("POST", "/api/conversations/{conversation_id}/voice-calls/{session_id}/recording"),
    # per-user notifications + presence + workspace name (read only)
    ("GET", "/api/notifications"), ("POST", "/api/notifications/read-all"),
    ("POST", "/api/notifications/{notification_id}/read"), ("DELETE", "/api/notifications"),
    ("DELETE", "/api/notifications/{notification_id}"), ("POST", "/api/presence/ping"),
    ("GET", "/api/settings/workspace"),
}


def _walk(dependant):
    for d in dependant.dependencies:
        yield d.call
        yield from _walk(d)


def _role_sets(route):
    """Allowed-role sets of every require_role() gate on the route."""
    out = []
    for call in _walk(route.dependant):
        if getattr(call, "__qualname__", "").startswith("require_role."):
            for cell in call.__closure__ or ():
                if isinstance(cell.cell_contents, set):
                    out.append(cell.cell_contents)
    return out


def _api_routes():
    for r in app.routes:
        if isinstance(r, APIRoute) and r.path.startswith("/api"):
            for m in r.methods - {"HEAD", "OPTIONS"}:
                yield m, r


def test_no_plain_authenticated_route_outside_allowlist():
    offenders = []
    for method, route in _api_routes():
        calls = list(_walk(route.dependant))
        if require_owner_any_valid in calls or _role_sets(route):
            continue
        if get_current_user in calls or get_any_valid_user in calls:
            if (method, route.path) not in AGENT_OK:
                offenders.append((method, route.path))
    assert not offenders, f"Routes reachable by agents without a role gate: {offenders}"


def _roles(method, path):
    for m, route in _api_routes():
        if m == method and route.path == path:
            sets = _role_sets(route)
            if require_owner_any_valid in list(_walk(route.dependant)):
                sets.append({TenantRole.owner})
            assert sets, f"{method} {path} has no role gate"
            return set.intersection(*sets)
    raise AssertionError(f"route not found: {method} {path}")


OWNER = {TenantRole.owner}
ADMIN_UP = {TenantRole.owner, TenantRole.admin}


def test_sensitive_routes_admit_only_intended_roles():
    expected = {
        ("POST", "/api/settings/members/invite"): OWNER,
        ("PUT", "/api/settings/members/{user_id}/role"): OWNER,
        ("DELETE", "/api/settings/members/{user_id}"): OWNER,
        ("DELETE", "/api/settings/members/invites/{invite_id}"): OWNER,
        ("GET", "/api/settings/members"): ADMIN_UP,
        ("PUT", "/api/settings/security"): OWNER,
        ("GET", "/api/settings/security"): ADMIN_UP,
        ("PUT", "/api/settings/notifications"): ADMIN_UP,
        ("PUT", "/api/settings/model"): OWNER,
        ("GET", "/api/model-options"): ADMIN_UP,
        ("POST", "/api/chatbot/api-key/regenerate"): OWNER,
        ("POST", "/api/conversations/{conversation_id}/transfer"): ADMIN_UP,
        ("DELETE", "/api/conversations/{conversation_id}"): ADMIN_UP,
        ("GET", "/api/team/stats"): OWNER,
        ("GET", "/api/audit/export"): OWNER,
        ("POST", "/api/onboarding/complete"): OWNER,
        ("POST", "/api/onboarding/step/{step}"): OWNER,
        ("PUT", "/api/onboarding/summary"): OWNER,
        ("DELETE", "/api/settings/workspace"): OWNER,
    }
    for (method, path), roles in expected.items():
        assert _roles(method, path) == roles, f"{method} {path}"


def test_invite_schema_can_never_grant_owner():
    import pytest
    from pydantic import ValidationError

    from app.schemas.frontend import FEChangeRoleRequest, FEInviteRequest

    assert FEInviteRequest(email="a@b.co").role == "agent"
    assert FEInviteRequest(email="a@b.co", role="admin").role == "admin"
    for bad in ("owner", "root", ""):
        with pytest.raises(ValidationError):
            FEInviteRequest(email="a@b.co", role=bad)
        with pytest.raises(ValidationError):
            FEChangeRoleRequest(role=bad)


def test_pq_is_per_tenant_and_needs_platform_keys():
    from app.config import settings

    saved = (
        settings.PQ_KEM_PUBLIC_KEY, settings.PQ_KEM_PRIVATE_KEY,
        settings.PQ_SIG_PUBLIC_KEY, settings.PQ_SIG_PRIVATE_KEY,
        settings.PQ_FILE_ENCRYPTION_ENABLED, settings.PQ_WIDGET_TOKEN_SIGNING_ENABLED,
    )
    try:
        settings.PQ_FILE_ENCRYPTION_ENABLED = False
        settings.PQ_WIDGET_TOKEN_SIGNING_ENABLED = False
        settings.PQ_KEM_PUBLIC_KEY = settings.PQ_KEM_PRIVATE_KEY = ""
        settings.PQ_SIG_PUBLIC_KEY = settings.PQ_SIG_PRIVATE_KEY = ""
        # No platform keys → a tenant opting in changes nothing (fails closed to legacy).
        assert not pq_crypto.file_encryption_ready(True)
        assert not pq_crypto.token_signing_ready(True)

        settings.PQ_KEM_PUBLIC_KEY, settings.PQ_KEM_PRIVATE_KEY = pq_crypto.generate_kem_keypair()
        settings.PQ_SIG_PUBLIC_KEY, settings.PQ_SIG_PRIVATE_KEY = pq_crypto.generate_sig_keypair()
        # Keys present: only opted-in tenants get PQ.
        assert not pq_crypto.file_encryption_ready(False)
        assert pq_crypto.file_encryption_ready(True)
        assert not pq_crypto.token_signing_ready(False)
        assert pq_crypto.token_signing_ready(True)

        from app.core.widget_auth import create_widget_session_token, decode_widget_session_token

        plain = create_widget_session_token("t", "c")
        signed = create_widget_session_token("t", "c", pq_enabled=True)
        assert pq_crypto.PQ_TOKEN_SEPARATOR not in plain
        assert pq_crypto.PQ_TOKEN_SEPARATOR in signed
        # Both verify regardless of the tenant's current toggle state.
        assert decode_widget_session_token(plain) is not None
        assert decode_widget_session_token(signed) is not None
    finally:
        (
            settings.PQ_KEM_PUBLIC_KEY, settings.PQ_KEM_PRIVATE_KEY,
            settings.PQ_SIG_PUBLIC_KEY, settings.PQ_SIG_PRIVATE_KEY,
            settings.PQ_FILE_ENCRYPTION_ENABLED, settings.PQ_WIDGET_TOKEN_SIGNING_ENABLED,
        ) = saved
