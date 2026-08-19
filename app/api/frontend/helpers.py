"""
Shared helpers for the frontend-compat API layer (/api/...).

Everything that translates between backend domain objects (Tenant, Document,
Subscription, ...) and the camelCase wire format the Nexus dashboard expects
lives here so the endpoint modules stay thin.
"""

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chatbot_config import ChatbotConfig
from app.models.document import Document, DocumentSource, DocumentStatus
from app.models.subscription import PlanName, Subscription
from app.models.tenant import Tenant
from app.models.tenant_quota import TenantQuota
from app.models.usage_count import UsageCount
from app.models.user import User
from app.services.notification_service import notify_tenant

# ── Plan mapping ──────────────────────────────────────────────────────────────
# Frontend plan ids: starter | growth | business — backend PlanName stays
# free | pro | pro_plus (its original names, kept as-is since it's a live
# Postgres enum — renaming it isn't worth the migration risk for a purely
# cosmetic mismatch). This dict is the one place that translation happens.

FE_TO_BE_PLAN: dict[str, PlanName] = {
    "starter": PlanName.free,
    "growth": PlanName.pro,
    "business": PlanName.pro_plus,
}
BE_TO_FE_PLAN: dict[PlanName, str] = {v: k for k, v in FE_TO_BE_PLAN.items()}

PLAN_PRICE_LABELS: dict[str, str] = {
    "starter": "$10 / month",
    "growth": "$22 / month",
    "business": "$105 / month",
}

PLAN_DISPLAY_NAMES: dict[str, str] = {"starter": "Starter", "growth": "Growth", "business": "Business"}

# Mirrors PLAN_QUOTAS in auth_service.py (messages/docs) plus a chunks
# ceiling the quota model doesn't track, so the frontend's "used / total"
# progress bars always have a finite denominator.
PLAN_DISPLAY_LIMITS: dict[str, dict[str, int]] = {
    "starter": {"messages": 500, "docs": 25, "urls": 25, "chunks": 2000},
    "growth": {"messages": 5000, "docs": 150, "urls": 150, "chunks": 12000},
    "business": {"messages": 12000, "docs": 1000, "urls": 1000, "chunks": 80000},
}

# ── Display formatting ────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _as_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

def time_ago(dt: Optional[datetime]) -> str:
    """Relative label matching the dashboard's mock data style: '2m ago', '3h ago', '2 days ago'."""
    if dt is None:
        return ""
    delta = _utcnow() - _as_aware(dt)
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days == 1:
        return "1 day ago"
    if days < 7:
        return f"{days} days ago"
    weeks = days // 7
    if weeks == 1:
        return "1 week ago"
    if weeks < 5:
        return f"{weeks} weeks ago"
    return format_date(dt)

def format_date(dt: datetime) -> str:
    """'Jul 28, 2026' — matches the frontend mock data."""
    return f"{_as_aware(dt).strftime('%b')} {_as_aware(dt).day}, {_as_aware(dt).year}"

def hhmm(dt: datetime) -> str:
    return _as_aware(dt).strftime("%H:%M")

def size_label(size_mb: float) -> str:
    if size_mb < 1:
        return f"{int(round(size_mb * 1024))} KB"
    return f"{round(size_mb, 1)} MB"

def initials(name: str) -> str:
    parts = [p for p in name.strip().split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()

def first_of_next_month() -> datetime:
    now = _utcnow()
    if now.month == 12:
        return now.replace(year=now.year + 1, month=1, day=1)
    return now.replace(month=now.month + 1, day=1)

def doc_status_to_fe(s: DocumentStatus) -> str:
    if s == DocumentStatus.ready:
        return "Indexed"
    if s == DocumentStatus.failed:
        return "Failed"
    return "Processing"  # pending + processing

def generate_widget_api_key() -> str:
    return "nx_live_sk_" + secrets.token_hex(16)

def generate_workspace_api_key() -> str:
    return "sk_live_" + secrets.token_hex(12)

def mask_key(raw: str) -> str:
    return f"{raw[:8]}{'•' * 12}{raw[-4:]}"

# ── DB fetch helpers ──────────────────────────────────────────────────────────

async def get_tenant(user: User, db: AsyncSession) -> Tenant:
    result = await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Tenant record not found.",
        )
    return tenant

async def get_tenant_and_subscription(user: User, db: AsyncSession) -> tuple[Tenant, Optional[Subscription]]:
    """
    One round trip instead of get_tenant() + get_subscription() run back to
    back — that pair sits on the hottest path in the app (every login,
    signup, OTP-verify, and Google sign-in response builds an FEUser via
    this), and each round trip pays full network latency against a remote
    Postgres (Neon), not a local one. A tenant has at most one subscription
    row (see get_subscription), so the outer join can't multiply rows.
    """
    result = await db.execute(
        select(Tenant, Subscription)
        .outerjoin(Subscription, Subscription.tenant_id == Tenant.id)
        .where(Tenant.id == user.tenant_id)
    )
    row = result.first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Tenant record not found.",
        )
    tenant, subscription = row
    return tenant, subscription

async def get_subscription(tenant_id: uuid.UUID, db: AsyncSession) -> Optional[Subscription]:
    result = await db.execute(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    )
    return result.scalar_one_or_none()

async def get_quota(tenant_id: uuid.UUID, db: AsyncSession) -> Optional[TenantQuota]:
    result = await db.execute(
        select(TenantQuota).where(TenantQuota.tenant_id == tenant_id)
    )
    return result.scalar_one_or_none()

async def get_current_usage(tenant_id: uuid.UUID, db: AsyncSession) -> Optional[UsageCount]:
    result = await db.execute(
        select(UsageCount)
        .where(UsageCount.tenant_id == tenant_id)
        .order_by(UsageCount.period_month.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()

async def increment_questions_used(tenant_id: uuid.UUID, db: AsyncSession) -> None:
    """
    Bump the current month's questions_used. Split out from the streaming
    chat endpoints' post-generation bookkeeping because FastAPI closes
    Depends(get_db) before a StreamingResponse body is iterated — the
    request-scoped session is already committed and closed by then, so
    callers must run this against a *fresh* session opened after the stream
    completes, not the one injected into the endpoint.

    Also the single choke point for the plan-usage-warning notification —
    every questions_used increment (dashboard test-chat and the public
    widget, streaming and non-streaming) already runs through here, so the
    80%/100% threshold check only needs to live in one place.
    """
    usage = await get_current_usage(tenant_id, db)
    if usage is None:
        return
    usage.questions_used = (usage.questions_used or 0) + 1
    await _maybe_warn_plan_usage(tenant_id, usage, db)

async def _maybe_warn_plan_usage(tenant_id: uuid.UUID, usage: UsageCount, db: AsyncSession) -> None:
    quota = await get_quota(tenant_id, db)
    if quota is None or quota.max_questions_per_month == -1:
        return
    ratio = usage.questions_used / quota.max_questions_per_month

    if ratio >= 1.0 and not usage.warned_100_percent:
        usage.warned_100_percent = True
        await notify_tenant(
            tenant_id, "plan_usage_warnings",
            "You've reached 100% of your plan's monthly message limit",
            "New chat messages will keep working, but consider upgrading so you don't run into a hard cap later.",
            "/dashboard/settings?tab=billing", db,
        )
    elif ratio >= 0.8 and not usage.warned_80_percent:
        usage.warned_80_percent = True
        await notify_tenant(
            tenant_id, "plan_usage_warnings",
            "You've used 80% of your plan's monthly message limit",
            "Consider upgrading before you hit your limit.",
            "/dashboard/settings?tab=billing", db,
        )

async def get_quota_and_usage(
    tenant_id: uuid.UUID, db: AsyncSession
) -> tuple[Optional[TenantQuota], Optional[UsageCount]]:
    """
    One round trip instead of get_quota() + get_current_usage() run back to
    back — same idea as get_tenant_and_subscription(). This pair sits
    directly in front of every chat message (test_chat, the public widget's
    /message), which is the one place round-trip count actually shows up as
    user-visible latency, not just aggregate DB load. AsyncSession can't run
    two queries concurrently on the same connection (asyncio.gather isn't
    safe here), so a join is the only way to actually cut this to one
    round trip rather than two sequential ones.
    """
    result = await db.execute(
        select(TenantQuota, UsageCount)
        .outerjoin(
            UsageCount,
            (UsageCount.tenant_id == TenantQuota.tenant_id),
        )
        .where(TenantQuota.tenant_id == tenant_id)
        .order_by(UsageCount.period_month.desc())
        .limit(1)
    )
    row = result.first()
    if row is None:
        # No TenantQuota row at all — fall back to a plain usage lookup so a
        # tenant without quota provisioning yet still gets its usage count.
        return None, await get_current_usage(tenant_id, db)
    return row[0], row[1]

async def get_active_documents(tenant_id: uuid.UUID, db: AsyncSession) -> list[Document]:
    result = await db.execute(
        select(Document)
        .where(Document.tenant_id == tenant_id, Document.is_active == True)
        .order_by(Document.created_at.desc())
    )
    return list(result.scalars().all())

def split_docs_and_urls(
    docs: list[Document],
) -> tuple[list[Document], list[Document], list[Document]]:
    """
    Scraped documents show up in the Knowledge page's URLs tab, FAQ entries
    in the FAQs tab, everything else (uploaded/sample) in Documents.
    """
    url_docs = [d for d in docs if d.source == DocumentSource.scraped]
    faq_docs = [d for d in docs if d.source == DocumentSource.faq]
    file_docs = [d for d in docs if d.source not in (DocumentSource.scraped, DocumentSource.faq)]
    return file_docs, url_docs, faq_docs

async def get_tenant_by_widget_api_key(
    key: str, db: AsyncSession
) -> Optional[tuple[Tenant, ChatbotConfig]]:
    """
    Resolve a tenant + its chatbot config from a *widget* API key
    (`config->deploy->apiKey`, generated by generate_widget_api_key()).

    This is the only lookup path for that key — used exclusively by the
    public widget router (app/api/public/widget.py). It is intentionally
    disjoint from get_current_user (dashboard JWT auth): a widget key can
    never authenticate a dashboard/knowledge-base/settings/billing request,
    and a dashboard login token can never authenticate a widget request.
    """
    if not key:
        return None
    result = await db.execute(
        select(Tenant, ChatbotConfig)
        .join(ChatbotConfig, ChatbotConfig.tenant_id == Tenant.id)
        .where(ChatbotConfig.config["deploy"]["apiKey"].astext == key)
    )
    row = result.first()
    return (row[0], row[1]) if row else None

def fe_plan_for_subscription(subscription: Optional[Subscription]) -> str:
    if subscription is None:
        return "free"
    return BE_TO_FE_PLAN.get(subscription.plan_name, "free")

def onboarding_completed(subscription: Optional[Subscription]) -> bool:
    return subscription is not None
