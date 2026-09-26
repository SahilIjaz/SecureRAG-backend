"""
Push-to-Talk (PTT) real-time signaling over a native FastAPI WebSocket.

Nexus has no Socket.IO/WebRTC infrastructure, so this module builds it from
scratch: a per-conversation connection registry and a small JSON message
protocol that relays WebRTC signaling (SDP offer/answer + ICE candidates)
between the two people on a *live* conversation — the visitor on the widget and
the agent who joined it on the dashboard.

The server never touches audio. It only:
  1. authenticates each socket (dashboard JWT for an agent, widget session
     token for a visitor),
  2. scopes it to one conversation ("room"),
  3. relays the signaling messages to the other party, and
  4. records a PTTSession row for history + duration.

Audio itself flows peer-to-peer via the browsers' RTCPeerConnection once the
offer/answer/ICE exchange (relayed here) completes.

Single-process, in-memory registry — fine for one uvicorn worker. Multi-worker
production would need the registry backed by Redis pub/sub; called out here so
it isn't a silent assumption.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from sqlalchemy import select

from app.core.security import decode_token
from app.core.widget_auth import decode_widget_session_token
from app.core.entitlements import get_entitlements
from app.database import AsyncSessionLocal
from app.models.conversation import Conversation
from app.models.ptt_session import PTTSession, PTTStatus
from app.models.subscription import Subscription

logger = logging.getLogger(__name__)

router = APIRouter()

# Participant keys within a conversation room.
AGENT = "agent"
VISITOR = "visitor"


class _Participant:
    def __init__(self, ws: WebSocket, kind: str, ident: str, name: str):
        self.ws = ws
        self.kind = kind          # "agent" | "visitor"
        self.ident = ident        # user id (agent) or "visitor" (widget)
        self.name = name

    @property
    def key(self) -> str:
        # One agent and one visitor per conversation room for PTT purposes.
        return self.kind


class PTTConnectionManager:
    """In-memory map of conversation_id -> {participant_key: _Participant}."""

    def __init__(self) -> None:
        self._rooms: dict[str, dict[str, _Participant]] = {}

    def add(self, conversation_id: str, p: _Participant) -> None:
        self._rooms.setdefault(conversation_id, {})[p.key] = p

    def remove(self, conversation_id: str, participant: _Participant) -> bool:
        """Remove *this* participant. Returns False (and does nothing) if the
        slot is now held by a newer socket of the same kind — e.g. the
        dashboard reconnected and the old socket's cleanup ran late. Removing
        by key alone used to evict the new socket, leaving the room agent-less
        and the visitor wrongly told the agent had left."""
        room = self._rooms.get(conversation_id)
        if not room or room.get(participant.key) is not participant:
            return False
        room.pop(participant.key, None)
        if not room:
            self._rooms.pop(conversation_id, None)
        return True

    def other(self, conversation_id: str, me_key: str) -> Optional[_Participant]:
        room = self._rooms.get(conversation_id, {})
        for key, p in room.items():
            if key != me_key:
                return p
        return None

    def get(self, conversation_id: str, key: str) -> Optional[_Participant]:
        return self._rooms.get(conversation_id, {}).get(key)


manager = PTTConnectionManager()


async def _authenticate(token: str, as_role: str) -> Optional[dict]:
    """Resolve a websocket's identity from its token.

    Returns a dict {conversation_id?, tenant_id, kind, ident, name} or None.
    An agent authenticates with a dashboard JWT and must name the conversation
    (validated below); a visitor authenticates with a widget session token that
    already encodes the conversation and tenant.
    """
    if as_role == VISITOR:
        payload = decode_widget_session_token(token)
        if not payload:
            return None
        return {
            "conversation_id": payload["conversation_id"],
            "tenant_id": payload["tenant_id"],
            "kind": VISITOR,
            "ident": VISITOR,
            "name": "Visitor",
        }
    # Agent path — dashboard access token.
    try:
        payload = decode_token(token)
    except Exception:
        return None
    if payload.get("type") != "access" or not payload.get("sub"):
        return None
    return {
        "conversation_id": None,  # supplied via query param, validated below
        "tenant_id": payload.get("tenant_id"),
        "kind": AGENT,
        "ident": str(payload["sub"]),
        "name": "Agent",
    }


async def _load_live_conversation(conversation_id: str, tenant_id: str) -> Optional[Conversation]:
    try:
        cid = uuid.UUID(conversation_id)
        tid = uuid.UUID(tenant_id)
    except (ValueError, TypeError):
        return None
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Conversation).where(
                Conversation.id == cid, Conversation.tenant_id == tid
            )
        )
        return result.scalar_one_or_none()


async def _tenant_has_ptt(tenant_id: str) -> bool:
    try:
        tid = uuid.UUID(tenant_id)
    except (ValueError, TypeError):
        return False
    async with AsyncSessionLocal() as db:
        sub = (await db.execute(
            select(Subscription).where(Subscription.tenant_id == tid)
        )).scalar_one_or_none()
    return get_entitlements(sub).push_to_talk


async def _send(ws: WebSocket, message: dict) -> None:
    try:
        await ws.send_json(message)
    except Exception:
        pass  # peer went away mid-send; disconnect handling will clean up


@router.websocket("/ws/ptt")
async def ptt_websocket(
    websocket: WebSocket,
    token: str = Query(...),
    as_role: str = Query(..., alias="as"),
    conversation_id: Optional[str] = Query(None, alias="conversationId"),
) -> None:
    await websocket.accept()

    identity = await _authenticate(token, as_role)
    if identity is None:
        await _send(websocket, {"type": "ptt_error", "message": "Unauthorized."})
        await websocket.close()
        return

    # Resolve which conversation this socket belongs to.
    conv_id = identity["conversation_id"] or conversation_id
    if not conv_id:
        await _send(websocket, {"type": "ptt_error", "message": "No conversation specified."})
        await websocket.close()
        return

    conversation = await _load_live_conversation(conv_id, identity["tenant_id"])
    if conversation is None:
        await _send(websocket, {"type": "ptt_error", "message": "Conversation not found."})
        await websocket.close()
        return

    # PTT is a paid feature — reject the socket if the plan doesn't include it.
    if not await _tenant_has_ptt(identity["tenant_id"]):
        await _send(websocket, {"type": "ptt_error", "message": "Push-to-talk isn't available on this plan."})
        await websocket.close()
        return

    participant = _Participant(websocket, identity["kind"], identity["ident"], identity["name"])
    manager.add(conv_id, participant)
    # Tell each side whether the other is already present, so the button can
    # enable itself only when both are on the line.
    other = manager.other(conv_id, participant.key)
    await _send(websocket, {"type": "ptt_ready", "peerPresent": other is not None})
    if other is not None:
        await _send(other.ws, {"type": "ptt_peer_joined"})

    try:
        while True:
            msg = await websocket.receive_json()
            await _handle(conv_id, identity["tenant_id"], participant, msg)
    except WebSocketDisconnect:
        pass
    except RuntimeError as e:
        # Starlette raises RuntimeError('WebSocket is not connected…') when a
        # send to a client that already vanished flipped the socket to
        # DISCONNECTED before our next receive. It's an ordinary hang-up,
        # not a bug — don't spam the log with a traceback for it.
        if "not connected" in str(e):
            logger.debug("PTT socket closed by peer (conv=%s kind=%s)", conv_id, participant.kind)
        else:
            logger.exception("PTT websocket error (conv=%s kind=%s)", conv_id, participant.kind)
    except Exception:
        logger.exception("PTT websocket error (conv=%s kind=%s)", conv_id, participant.kind)
    finally:
        # Only announce "peer left" if we really vacated the slot — if a newer
        # socket of ours already took it over, the peer never lost us.
        if manager.remove(conv_id, participant):
            peer = manager.other(conv_id, participant.key)
            if peer is not None:
                await _send(peer.ws, {"type": "ptt_peer_left"})


async def _handle(conv_id: str, tenant_id: str, me: _Participant, msg: dict) -> None:
    mtype = msg.get("type")
    peer = manager.other(conv_id, me.key)

    if mtype == "ptt_request":
        if peer is None:
            # Soft failure — the client drops back to "waiting" rather than
            # treating this as fatal (code is what the client keys on).
            who = "agent" if me.kind == VISITOR else "visitor"
            await _send(me.ws, {
                "type": "ptt_error",
                "code": "peer_absent",
                "message": f"The {who} isn't on the chat right now.",
            })
            return
        session_id = uuid.uuid4().hex
        async with AsyncSessionLocal() as db:
            db.add(PTTSession(
                session_id=session_id,
                tenant_id=uuid.UUID(tenant_id),
                conversation_id=uuid.UUID(conv_id),
                initiator_id=me.ident, initiator_kind=me.kind,
                target_id=peer.ident, target_kind=peer.kind,
                status=PTTStatus.initiated,
            ))
            await db.commit()
        await _send(peer.ws, {"type": "ptt_incoming", "sessionId": session_id, "fromName": me.name, "fromKind": me.kind})
        await _send(me.ws, {"type": "ptt_request_sent", "sessionId": session_id})

    elif mtype == "ptt_accept":
        session_id = msg.get("sessionId")
        if peer is None:
            # Caller vanished between ringing and answering — don't leave the
            # answering side stuck in "connecting".
            await _set_status(session_id, PTTStatus.failed)
            who = "agent" if me.kind == VISITOR else "visitor"
            await _send(me.ws, {
                "type": "ptt_error", "code": "peer_absent",
                "message": f"The {who} left before the call connected.",
            })
            return
        await _set_status(session_id, PTTStatus.active, started=True)
        await _send(peer.ws, {"type": "ptt_accepted", "sessionId": session_id})
        await _send(me.ws, {"type": "ptt_session_started", "sessionId": session_id})

    elif mtype == "ptt_reject":
        session_id = msg.get("sessionId")
        await _set_status(session_id, PTTStatus.failed)
        if peer is not None:
            await _send(peer.ws, {"type": "ptt_rejected", "sessionId": session_id})

    # ── WebRTC signaling relay (server just forwards to the other party) ──
    elif mtype in ("ptt_offer", "ptt_answer", "ptt_ice"):
        if peer is not None:
            await _send(peer.ws, msg)

    # ── On-air indicators (hold-to-talk) ──
    elif mtype == "ptt_start_talking":
        if peer is not None:
            await _send(peer.ws, {"type": "ptt_talking", "isTalking": True, "sessionId": msg.get("sessionId")})
    elif mtype == "ptt_stop_talking":
        if peer is not None:
            await _send(peer.ws, {"type": "ptt_talking", "isTalking": False, "sessionId": msg.get("sessionId")})

    elif mtype == "ptt_end":
        session_id = msg.get("sessionId")
        duration = await _set_status(session_id, PTTStatus.ended, ended=True)
        payload = {"type": "ptt_ended", "sessionId": session_id, "duration": duration}
        await _send(me.ws, payload)
        if peer is not None:
            await _send(peer.ws, payload)


async def _set_status(session_id: Optional[str], status: PTTStatus, *,
                      started: bool = False, ended: bool = False) -> Optional[int]:
    """Update a session's status; on end, compute and store the duration.
    Returns the duration in seconds when ending, else None."""
    if not session_id:
        return None
    async with AsyncSessionLocal() as db:
        sess = (await db.execute(
            select(PTTSession).where(PTTSession.session_id == session_id)
        )).scalar_one_or_none()
        if sess is None:
            return None
        now = datetime.now(timezone.utc)
        sess.status = status
        if started and sess.started_at is None:
            sess.started_at = now
        duration = None
        if ended:
            sess.ended_at = now
            if sess.started_at is not None:
                start = sess.started_at if sess.started_at.tzinfo else sess.started_at.replace(tzinfo=timezone.utc)
                duration = max(0, int((now - start).total_seconds()))
                sess.duration_seconds = duration
        await db.commit()
        return duration
