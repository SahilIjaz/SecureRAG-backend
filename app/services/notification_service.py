"""
Notification service — the single place that decides "did this happen" vs
"does the tenant want to hear about it."

notify_tenant() is the one entry point callers should use: it always writes
an in-app Notification row (the dashboard bell — unconditional, see
models/notification.py), then additionally emails the tenant's account
owner if their NotificationSetting has that type enabled. Callers never
touch NotificationSetting or the email layer directly.

weekly_summary_loop() is the one recurring background job — started once
from the app lifespan, same fire-and-forget pattern as
indexing_service.stale_document_sweep_loop().
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.email import send_email
from app.database import AsyncSessionLocal
from app.models.conversation import Conversation
from app.models.notification import Notification
from app.models.tenant import Tenant
from app.models.tenant_settings import NotificationSetting
from app.models.user import User

logger = logging.getLogger(__name__)

_QUESTION_PREVIEW_LEN = 100

def conversation_alert_copy(notif_type: str, question: str) -> tuple[str, str]:
    """(title, body) for a conversation-triggered notification — shared by
    the public widget and the dashboard's "simulate real visitor" test chat,
    the two places a real visitor/simulated turn can hit a handoff or
    knowledge gap and call notify_tenant()."""
    preview = question.strip()
    if len(preview) > _QUESTION_PREVIEW_LEN:
        preview = preview[:_QUESTION_PREVIEW_LEN - 3] + "..."
    if notif_type == "knowledge_gaps_detected":
        return "A customer asked something you haven't documented", f'"{preview}"'
    return "A conversation needs your attention", f'"{preview}"'

async def notify_tenant(
    tenant_id: uuid.UUID,
    notif_type: str,
    title: str,
    body: Optional[str],
    link: Optional[str],
    db: AsyncSession,
) -> None:
    """
    Record a notification for a tenant and email it if enabled. Does not
    commit — participates in whatever transaction the caller is already in
    (matches helpers.increment_questions_used's own convention).
    """
    db.add(Notification(tenant_id=tenant_id, type=notif_type, title=title, body=body, link=link))

    result = await db.execute(
        select(NotificationSetting).where(NotificationSetting.tenant_id == tenant_id)
    )
    setting = result.scalar_one_or_none()
    # No settings row yet (tenant never opened Settings -> Notifications) —
    # fall back to the same defaults NotificationSetting's columns use.
    email_enabled = getattr(setting, notif_type) if setting is not None else True
    if not email_enabled:
        return

    result = await db.execute(
        select(User.email, User.full_name).where(User.tenant_id == tenant_id).limit(1)
    )
    row = result.first()
    if row is None or not row[0]:
        return
    recipient_email, full_name = row

    html = _build_notification_html(full_name or "there", title, body, link)
    text = f"{title}\n\n{body or ''}".strip()
    try:
        await send_email(recipient_email, subject=title, html_content=html, text_content=text)
    except Exception:
        logger.exception("Failed to send notification email (type=%s) for tenant %s", notif_type, tenant_id)

def _build_notification_html(full_name: str, title: str, body: Optional[str], link: Optional[str]) -> str:
    cta = (
        f'<p style="margin-top:24px;"><a href="{settings.FRONTEND_URL.rstrip("/")}{link}" '
        'style="background:#0f2027;color:#fff;padding:10px 20px;border-radius:6px;'
        'text-decoration:none;font-size:14px;">Open dashboard</a></p>'
        if link else ""
    )
    body_html = f"<p>{body}</p>" if body else ""
    return f"""
    <!DOCTYPE html>
    <html><head><meta charset="UTF-8"/></head>
    <body style="font-family:Arial,sans-serif;background:#f4f4f4;margin:0;padding:0;">
      <div style="max-width:500px;margin:40px auto;background:#fff;border-radius:8px;
                  overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1);">
        <div style="background:#0f2027;padding:24px 32px;">
          <h1 style="color:#fff;margin:0;font-size:20px;">SecureRAG++</h1>
        </div>
        <div style="padding:32px;">
          <p>Hi <strong>{full_name}</strong>,</p>
          <p style="font-size:16px;font-weight:600;">{title}</p>
          {body_html}
          {cta}
        </div>
        <div style="background:#f4f4f4;padding:16px 32px;text-align:center;color:#aaa;font-size:12px;">
          &copy; 2026 SecureRAG++. You can turn these off in Settings &rarr; Notifications.
        </div>
      </div>
    </body></html>
    """

# ── Weekly summary ──────────────────────────────────────────────────────────

_WEEKLY_SUMMARY_HOUR_UTC = 9  # Monday, 9am UTC — matches the settings tab's "Monday morning recap" copy

def _seconds_until_next_monday(now: Optional[datetime] = None) -> float:
    now = now or datetime.now(timezone.utc)
    days_ahead = (7 - now.weekday()) % 7  # datetime.weekday(): Monday == 0
    target = (now + timedelta(days=days_ahead)).replace(
        hour=_WEEKLY_SUMMARY_HOUR_UTC, minute=0, second=0, microsecond=0
    )
    if target <= now:
        target += timedelta(days=7)
    return (target - now).total_seconds()

async def weekly_summary_loop() -> None:
    """
    Sleeps until the next Monday morning before doing any work — unlike
    stale_document_sweep_loop's short interval, firing this immediately on
    every process restart would re-send the week's summary any time the
    server happens to redeploy.
    """
    while True:
        await asyncio.sleep(_seconds_until_next_monday())
        try:
            await send_weekly_summaries()
        except Exception:
            logger.exception("Weekly summary send failed")

async def send_weekly_summaries() -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Tenant.id, NotificationSetting.weekly_summary)
            .outerjoin(NotificationSetting, NotificationSetting.tenant_id == Tenant.id)
            .where(Tenant.is_active == True, Tenant.workspace_name != "__pending__")
        )
        tenants = [(tid, enabled if enabled is not None else True) for tid, enabled in result.all()]

        for tenant_id, enabled in tenants:
            if not enabled:
                continue
            try:
                await _send_one_weekly_summary(tenant_id, db)
            except Exception:
                logger.exception("Weekly summary failed for tenant %s", tenant_id)
        await db.commit()

async def _send_one_weekly_summary(tenant_id: uuid.UUID, db: AsyncSession) -> None:
    since = datetime.now(timezone.utc) - timedelta(days=7)
    result = await db.execute(
        select(Conversation.status, func.count())
        .where(Conversation.tenant_id == tenant_id, Conversation.created_at >= since)
        .group_by(Conversation.status)
    )
    counts = dict(result.all())
    total = sum(counts.values())
    if total == 0:
        return  # nothing happened this week — don't email an empty summary

    resolved = counts.get("Resolved", 0)
    handed_off = counts.get("Handed off", 0)
    body = (
        f"{total} conversation{'s' if total != 1 else ''} in the last 7 days — "
        f"{resolved} resolved, {handed_off} handed off to a human."
    )
    await notify_tenant(
        tenant_id, "weekly_summary", "Your weekly summary is ready", body, "/dashboard", db
    )
