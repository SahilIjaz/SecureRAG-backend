"""
Real-time notification push (app/core/ws_notifications.py).

Pure unit tests — no DB, no running ASGI server. `_authenticate` only ever
decodes a JWT (see its own docstring for why it skips the DB round trip
get_current_user does), and `NotificationConnectionManager` is a plain
in-memory registry, so both are exercised directly against fakes.

Run with:  PYTHONPATH=. ./venv/bin/pytest tests/test_notifications_ws.py -v
"""

from __future__ import annotations

import uuid

import pytest

from app.core.security import create_access_token, create_refresh_token
from app.core.ws_notifications import NotificationConnectionManager, _authenticate


class _FakeWebSocket:
    """Stands in for a starlette WebSocket — only the one method this
    module ever calls on it."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        if self.fail:
            raise RuntimeError("peer vanished")
        self.sent.append(message)


# ── _authenticate ────────────────────────────────────────────────────────────

def test_authenticate_rejects_garbage_token():
    assert _authenticate("not-a-jwt") is None


def test_authenticate_rejects_a_refresh_token():
    # A refresh token is a validly-signed JWT, just the wrong `type` claim —
    # this must not be accepted as a substitute for an access token.
    token, _jti, _expiry = create_refresh_token({"sub": str(uuid.uuid4())})
    assert _authenticate(token) is None


def test_authenticate_rejects_an_access_token_missing_tenant_id():
    token = create_access_token({"sub": str(uuid.uuid4())})
    assert _authenticate(token) is None


def test_authenticate_accepts_a_valid_access_token():
    user_id = str(uuid.uuid4())
    tenant_id = str(uuid.uuid4())
    token = create_access_token({"sub": user_id, "tenant_id": tenant_id})

    identity = _authenticate(token)

    assert identity == {"user_id": user_id, "tenant_id": tenant_id}


# ── NotificationConnectionManager ────────────────────────────────────────────

def test_manager_add_and_remove_tracks_sockets_per_tenant():
    manager = NotificationConnectionManager()
    tenant_a, tenant_b = str(uuid.uuid4()), str(uuid.uuid4())
    ws_a, ws_b = _FakeWebSocket(), _FakeWebSocket()

    manager.add(tenant_a, ws_a)
    manager.add(tenant_b, ws_b)
    assert manager._tenants[tenant_a] == {ws_a}
    assert manager._tenants[tenant_b] == {ws_b}

    manager.remove(tenant_a, ws_a)
    # The tenant's whole entry is dropped once its last socket leaves —
    # a stale empty set would otherwise sit in the dict forever.
    assert tenant_a not in manager._tenants
    assert tenant_b in manager._tenants


def test_manager_remove_is_a_no_op_for_an_unknown_tenant_or_socket():
    manager = NotificationConnectionManager()
    # Must not raise even though neither the tenant nor the socket was ever added.
    manager.remove(str(uuid.uuid4()), _FakeWebSocket())


@pytest.mark.asyncio
async def test_broadcast_reaches_every_socket_for_the_tenant_and_no_other():
    manager = NotificationConnectionManager()
    tenant_a, tenant_b = str(uuid.uuid4()), str(uuid.uuid4())
    ws_a1, ws_a2, ws_b = _FakeWebSocket(), _FakeWebSocket(), _FakeWebSocket()
    manager.add(tenant_a, ws_a1)
    manager.add(tenant_a, ws_a2)
    manager.add(tenant_b, ws_b)

    message = {"type": "notification", "notification": {"id": "1"}}
    await manager.broadcast(tenant_a, message)

    assert ws_a1.sent == [message]
    assert ws_a2.sent == [message]
    assert ws_b.sent == []  # a different tenant's socket never sees this


@pytest.mark.asyncio
async def test_broadcast_drops_a_failing_socket_without_affecting_others():
    manager = NotificationConnectionManager()
    tenant = str(uuid.uuid4())
    dead, alive = _FakeWebSocket(fail=True), _FakeWebSocket()
    manager.add(tenant, dead)
    manager.add(tenant, alive)

    # Must not raise even though `dead.send_json` raises internally.
    await manager.broadcast(tenant, {"type": "notification"})

    assert alive.sent == [{"type": "notification"}]
    # broadcast() itself doesn't evict a dead socket (that's the receive
    # loop's job on disconnect) — just confirm the registry is untouched.
    assert manager._tenants[tenant] == {dead, alive}
