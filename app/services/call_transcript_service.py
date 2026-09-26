"""
Voice-call transcripts.

The agent's browser records a live voice call as two separate tracks on one
shared clock — its own microphone and the visitor's incoming audio — and
uploads both when the call ends (see usePtt.ts / ConversationDetailPanel.tsx).
Recording two tracks instead of one mix means speaker attribution is exact:
everything on track A is the agent, everything on track B is the visitor, so
we never have to guess who said what.

Pipeline (runs in the background after the upload request returns):
  1. transcribe each track with Gemini → timestamped segments
  2. merge the two segment lists by start time and label each line with the
     speaker's real name → "Ali: hy" / "Ahmad: hello"
  3. persist the transcript on the PTTSession row
  4. post it into the conversation as a message so it shows on the dashboard
     and in the widget's history
  5. email it to the workspace owners/admins, the agent on the call, and the
     visitor if they left an email address
"""

from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.email import send_email
from app.database import AsyncSessionLocal
from app.models.conversation import Conversation, ConversationMessage
from app.models.ptt_session import PTTSession
from app.models.tenant import Tenant
from app.models.tenant_user import TenantRole, TenantUser
from app.models.user import User

logger = logging.getLogger(__name__)

# Gemini's inline request limit is 20 MB; anything bigger goes through the
# Files API. Audio arrives as 16 kHz mono 16-bit WAV (~1.9 MB/min) so a call
# under ~8 minutes stays inline.
_INLINE_LIMIT_BYTES = 15 * 1024 * 1024
_TRANSCRIBE_TIMEOUT_SECONDS = 180


@dataclass
class Segment:
    start: float
    end: float
    text: str


# ── Gemini transcription ─────────────────────────────────────────────────────

_PROMPT = (
    "Transcribe the speech in this audio recording verbatim.\n"
    "- Keep the language exactly as spoken. If the speaker uses Urdu, Hindi or "
    "Punjabi, write it in Roman (Latin) script the way people type it in chat "
    "(for example: 'aap kaise hain', 'theek hai').\n"
    "- Split the speech into short utterances (one sentence or thought each).\n"
    "- Ignore silence, background noise and filler like 'um'.\n"
    "- If there is no intelligible speech at all, return an empty list.\n\n"
    "Return ONLY a JSON array, no prose, in this exact shape:\n"
    '[{"start": <seconds from the beginning, number>, "end": <seconds, number>, "text": "<utterance>"}]'
)


def _parse_segments(raw: str) -> list[Segment]:
    raw = raw.strip()
    # Tolerate ```json fences if the model adds them despite the JSON mime type.
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.MULTILINE).strip()
    if not raw:
        return []
    data = json.loads(raw)
    if isinstance(data, dict):  # some models wrap: {"segments": [...]}
        data = data.get("segments") or data.get("transcript") or []
    out: list[Segment] = []
    for item in data or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(item.get("start") or 0.0)
            end = float(item.get("end") or start)
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        out.append(Segment(start=max(0.0, start), end=max(start, end), text=text))
    out.sort(key=lambda s: s.start)
    return out


async def transcribe_track(audio: bytes, mime_type: str) -> list[Segment]:
    """Transcribe one audio track into timestamped segments. Tries each model
    in the configured Gemini chain; raises if all of them fail."""
    from google import genai
    from google.genai import types as genai_types

    if not settings.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    client = genai.Client(api_key=settings.GEMINI_API_KEY)

    # Large recordings go through the Files API instead of inline bytes.
    if len(audio) > _INLINE_LIMIT_BYTES:
        uploaded = await client.aio.files.upload(
            file=io.BytesIO(audio),
            config=genai_types.UploadFileConfig(mime_type=mime_type, display_name="call-track"),
        )
        audio_part = uploaded
    else:
        audio_part = genai_types.Part.from_bytes(data=audio, mime_type=mime_type)

    last_error: Optional[Exception] = None
    for model in settings.GEMINI_MODEL_CHAIN:
        try:
            response = await client.aio.models.generate_content(
                model=model,
                contents=[audio_part, _PROMPT],
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                    http_options=genai_types.HttpOptions(timeout=_TRANSCRIBE_TIMEOUT_SECONDS * 1000),
                ),
            )
            return _parse_segments(response.text or "")
        except Exception as e:  # noqa: BLE001 — fall through to the next model
            last_error = e
            logger.warning("Transcription with %s failed: %s", model, e)
    raise RuntimeError(f"All Gemini models failed to transcribe: {last_error}")


# ── Merge + format ───────────────────────────────────────────────────────────

def build_transcript(
    agent_segments: list[Segment],
    visitor_segments: list[Segment],
    agent_name: str,
    visitor_name: str,
) -> str:
    """Interleave both speakers' utterances by time and render
    'Name: text' lines. Consecutive lines from the same speaker are kept
    separate (one utterance per line) — that's the format the transcript is
    shared in."""
    labelled = [(s.start, agent_name, s.text) for s in agent_segments] + [
        (s.start, visitor_name, s.text) for s in visitor_segments
    ]
    labelled.sort(key=lambda t: t[0])
    return "\n".join(f"{name}: {text}" for _, name, text in labelled)


def _format_duration(seconds: Optional[int]) -> str:
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 60}:{seconds % 60:02d}"


# ── Pipeline ─────────────────────────────────────────────────────────────────

async def _set_status(db: AsyncSession, session: PTTSession, status: str) -> None:
    session.transcript_status = status
    await db.commit()


async def process_call_recording(
    session_id: str,
    agent_audio: Optional[bytes],
    agent_mime: str,
    visitor_audio: Optional[bytes],
    visitor_mime: str,
) -> None:
    """Background job: transcribe, store, post to the conversation, email."""
    async with AsyncSessionLocal() as db:
        session = (await db.execute(
            select(PTTSession).where(PTTSession.session_id == session_id)
        )).scalar_one_or_none()
        if session is None:
            logger.warning("Call recording for unknown session %s", session_id)
            return

        convo = (await db.execute(
            select(Conversation).where(Conversation.id == session.conversation_id)
        )).scalar_one_or_none()
        tenant = (await db.execute(
            select(Tenant).where(Tenant.id == session.tenant_id)
        )).scalar_one_or_none()
        if convo is None or tenant is None:
            await _set_status(db, session, "failed")
            return

        # Who was the agent on the call? The agent's user id is recorded as
        # initiator or target depending on who placed it.
        agent_user_id = session.initiator_id if session.initiator_kind == "agent" else session.target_id
        agent_user: Optional[User] = None
        try:
            if agent_user_id:
                agent_user = (await db.execute(
                    select(User).where(User.id == uuid.UUID(agent_user_id))
                )).scalar_one_or_none()
        except ValueError:
            agent_user = None

        agent_name = (agent_user.full_name if agent_user else None) or "Agent"
        visitor_name = convo.visitor_name or "Visitor"

        await _set_status(db, session, "processing")

        # ── 1. Transcribe both tracks concurrently ──
        async def _safe(audio: Optional[bytes], mime: str, who: str) -> list[Segment]:
            if not audio:
                return []
            try:
                return await transcribe_track(audio, mime)
            except Exception:
                logger.exception("Transcribing the %s track failed (session %s)", who, session_id)
                return []

        agent_segments, visitor_segments = await asyncio.gather(
            _safe(agent_audio, agent_mime, "agent"),
            _safe(visitor_audio, visitor_mime, "visitor"),
        )

        transcript = build_transcript(agent_segments, visitor_segments, agent_name, visitor_name)
        if not transcript.strip():
            # Nothing intelligible — e.g. a two-second accidental call.
            session.transcript = ""
            await _set_status(db, session, "skipped")
            logger.info("Call %s had no transcribable speech", session_id)
            return

        # ── 2. Persist + post into the conversation ──
        session.transcript = transcript
        duration = _format_duration(session.duration_seconds)
        message_text = f"📞 Voice call transcript ({duration})\n\n{transcript}"
        db.add(ConversationMessage(conversation_id=convo.id, role="agent", text=message_text))
        session.transcript_status = "done"
        await db.commit()

        # ── 3. Recipients ──
        owner_rows = (await db.execute(
            select(User.email, User.full_name)
            .join(TenantUser, TenantUser.user_id == User.id)
            .where(
                TenantUser.tenant_id == tenant.id,
                TenantUser.role.in_([TenantRole.owner, TenantRole.admin]),
            )
        )).all()

        business_name = getattr(tenant, "workspace_name", None) or settings.APP_NAME
        recipients: dict[str, tuple[str, str]] = {}  # email → (display name, audience)
        for email, name in owner_rows:
            if email:
                recipients[email.lower()] = (name or "there", "team")
        if agent_user and agent_user.email:
            recipients.setdefault(agent_user.email.lower(), (agent_user.full_name or "there", "team"))
        if convo.visitor_email:
            recipients.setdefault(convo.visitor_email.lower(), (visitor_name, "visitor"))

    # ── 4. Email (outside the DB session — nothing more to write) ──
    when = (session.started_at or datetime.now(timezone.utc)).strftime("%d %b %Y, %H:%M UTC")
    reply_to_email = agent_user.email if agent_user else (owner_rows[0][0] if owner_rows else None)
    reply_to_name = agent_user.full_name if agent_user else (owner_rows[0][1] if owner_rows else None)

    for email, (display_name, audience) in recipients.items():
        try:
            await _send_transcript_email(
                to_email=email,
                to_name=display_name,
                audience=audience,
                business_name=business_name,
                agent_name=agent_name,
                visitor_name=visitor_name,
                when=when,
                duration=duration,
                transcript=transcript,
                conversation_id=str(convo.id),
                reply_to_email=reply_to_email,
                reply_to_name=reply_to_name,
            )
        except Exception:
            logger.exception("Failed to email call transcript to %s", email)


# ── Email ────────────────────────────────────────────────────────────────────

async def _send_transcript_email(
    *,
    to_email: str,
    to_name: str,
    audience: str,              # "team" (owner/admin/agent) | "visitor"
    business_name: str,
    agent_name: str,
    visitor_name: str,
    when: str,
    duration: str,
    transcript: str,
    conversation_id: str,
    reply_to_email: Optional[str],
    reply_to_name: Optional[str],
) -> None:
    subject = (
        f"Your call with {business_name} — transcript"
        if audience == "visitor"
        else f"Call transcript: {agent_name} & {visitor_name} · {when}"
    )

    intro = (
        f"Here's a written copy of your voice call with {business_name}."
        if audience == "visitor"
        else f"Here's the transcript of the voice call between {agent_name} and {visitor_name}."
    )
    text_body = (
        f"{intro}\n\n"
        f"When: {when}\nDuration: {duration}\n\n"
        f"{transcript}\n"
    )

    lines_html = "".join(
        _line_html(line, agent_name) for line in transcript.splitlines() if line.strip()
    )
    cta = (
        ""
        if audience == "visitor"
        else (
            f'<p style="margin-top:24px;"><a href="{settings.FRONTEND_URL.rstrip("/")}/dashboard/conversations?id={conversation_id}" '
            'style="background:#0f2027;color:#fff;padding:10px 20px;border-radius:6px;'
            'text-decoration:none;font-size:14px;">Open conversation</a></p>'
        )
    )
    footer = (
        "Just reply to this email if you have any follow-up questions."
        if audience == "visitor"
        else "This transcript was generated automatically from the call recording."
    )

    html_body = f"""
    <!DOCTYPE html>
    <html><head><meta charset="UTF-8"/></head>
    <body style="font-family:Arial,sans-serif;background:#f4f4f4;margin:0;padding:0;">
      <div style="max-width:560px;margin:40px auto;background:#fff;border-radius:8px;
                  overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1);">
        <div style="background:#0f2027;padding:24px 32px;">
          <h1 style="color:#fff;margin:0;font-size:20px;">{html.escape(business_name)}</h1>
        </div>
        <div style="padding:32px;">
          <p>Hi <strong>{html.escape(to_name)}</strong>,</p>
          <p>{html.escape(intro)}</p>
          <table style="font-size:13px;color:#555;margin:12px 0 20px;border-collapse:collapse;">
            <tr><td style="padding:2px 12px 2px 0;">When</td><td>{html.escape(when)}</td></tr>
            <tr><td style="padding:2px 12px 2px 0;">Duration</td><td>{html.escape(duration)}</td></tr>
          </table>
          <div style="background:#f7f7f7;border-radius:8px;padding:16px 20px;font-size:14px;line-height:1.7;">
            {lines_html}
          </div>
          {cta}
          <p style="color:#888;font-size:13px;margin-top:24px;">{footer}</p>
        </div>
      </div>
    </body></html>
    """

    await send_email(
        to_email,
        subject=subject,
        html_content=html_body,
        text_content=text_body,
        from_name=business_name if audience == "visitor" else None,
        reply_to=reply_to_email if audience == "visitor" else None,
        reply_to_name=reply_to_name if audience == "visitor" else None,
    )


def _line_html(line: str, agent_name: str) -> str:
    """'Name: text' → bold name, agent lines tinted so the two voices scan apart."""
    name, sep, text = line.partition(": ")
    if not sep:
        return f"<div>{html.escape(line)}</div>"
    color = "#0f6b3a" if name == agent_name else "#0f2027"
    return (
        f'<div><strong style="color:{color};">{html.escape(name)}:</strong> '
        f"{html.escape(text)}</div>"
    )
