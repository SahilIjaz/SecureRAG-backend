"""
Real-time push for the dashboard notification bell, over a native FastAPI
WebSocket at /ws/notifications — the same hand-rolled approach as PTT
signaling (see app/api/ptt_ws.py), applied to a much simpler problem: there
is no peer-to-peer relay here, just "tell every open dashboard tab for this
tenant that something changed."

Notifications remain DB-backed and listable via GET /api/notifications (see
app/api/frontend/notifications.py) — that stays the source of truth. This
module is a push *transport* layered on top of it, not a replacement: the
client treats an incoming push as "go re-fetch," never as the final word,
so a missed or out-of-order push can never desync the bell from the DB (see
NotificationBell's onMessage handler on the frontend).

notification_service.notify_tenant() best-effort-broadcasts the same
notification it just added to the session to every socket currently open
for that tenant, via fire_and_forget — mirroring the existing fire-and-forget
email send in that same function. Both accept the same small pre-commit
race (the caller's transaction could still roll back after the push/email
already went out); that tradeoff is already made for email today, so this
keeps the two delivery paths consistent rather than introducing a stricter
rule for one and not the other.

Single-process, in-memory registry — same documented limitation as PTT:
fine for one uvicorn worker, needs Redis pub/sub to fan out across multiple
workers. Dead-connection cleanup relies on the websocket protocol's own
ping/pong (handled by uvicorn, not application code) tripping a disconnect,
same as PTT — no extra heartbeat loop here either, for the same reason.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core.security import decode_token

logger = logging.getLogger(__name__)

router = APIRouter()


class NotificationConnectionManager:
    """In-memory map of tenant_id -> the set of open dashboard sockets for
    that tenant. Every member of a tenant currently shares one notification
    feed (notify_tenant() has no per-user targeting yet, only the whole-tenant
    vs. one-user split already present in the Notification model) — so a
    broadcast simply fans out to every socket a tenant has open, and it is
    each client's own GET /api/notifications that applies the per-user
    visibility filter when it re-fetches."""

    def __init__(self) -> None:
        self._tenants: dict[str, set[WebSocket]] = {}

    def add(self, tenant_id: str, ws: WebSocket) -> None:
        self._tenants.setdefault(tenant_id, set()).add(ws)

    def remove(self, tenant_id: str, ws: WebSocket) -> None:
        sockets = self._tenants.get(tenant_id)
        if not sockets:
            return
        sockets.discard(ws)
        if not sockets:
            self._tenants.pop(tenant_id, None)

    async def broadcast(self, tenant_id: str, message: dict[str, Any]) -> None:
        """Best-effort fan-out. A send failure (peer gone, buffer full, ...)
        just drops that one socket from this call — the connection's own
        receive loop notices the disconnect and cleans up the registry entry
        on its own; a stale registry entry here would only ever cost one
        extra failed send, never a leak."""
        for ws in list(self._tenants.get(tenant_id, ())):
            try:
                await ws.send_json(message)
            except Exception:
                pass


manager = NotificationConnectionManager()


def _authenticate(token: str) -> Optional[dict]:
    """Dashboard-only (a widget visitor has no notification bell). Resolves
    straight from the access token's claims, without the DB round trip
    get_current_user does — same tradeoff as PTT's agent auth path: a
    frequently-reconnecting socket shouldn't pay a user-row lookup on every
    attempt. A revoked/deactivated session simply stops being able to open
    new sockets the next time its (short-lived) access token is checked
    elsewhere; this isn't the layer that enforces that."""
    try:
        payload = decode_token(token)
    except Exception:
        return None
    if payload.get("type") != "access" or not payload.get("sub") or not payload.get("tenant_id"):
        return None
    return {"user_id": str(payload["sub"]), "tenant_id": str(payload["tenant_id"])}


@router.websocket("/ws/notifications")
async def notifications_websocket(websocket: WebSocket, token: str = Query(...)) -> None:
    await websocket.accept()

    identity = _authenticate(token)
    if identity is None:
        await websocket.close(code=4401)
        return

    tenant_id = identity["tenant_id"]
    manager.add(tenant_id, websocket)
    try:
        while True:
            # Nothing the client sends is acted on — this socket is push-only
            # in one direction. receive_text() just blocks until the client
            # disconnects (or sends a stray keepalive ping), which is enough
            # to detect the hang-up without a bespoke ping/pong protocol.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except RuntimeError as e:
        # Same ordinary-hang-up case ptt_ws.py calls out: Starlette raises
        # this when a send to an already-vanished client flips the socket to
        # DISCONNECTED before our next receive — not a bug.
        if "not connected" in str(e):
            logger.debug("Notifications socket closed by client (tenant=%s)", tenant_id)
        else:
            logger.exception("Notifications websocket error (tenant=%s)", tenant_id)
    except Exception:
        logger.exception("Notifications websocket error (tenant=%s)", tenant_id)
    finally:
        manager.remove(tenant_id, websocket)
