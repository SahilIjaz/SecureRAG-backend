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

from app.models.document import Document, DocumentSource, DocumentStatus
from app.models.subscription import PlanName, Subscription
from app.models.tenant import Tenant
from app.models.tenant_quota import TenantQuota
from app.models.usage_count import UsageCount
from app.models.user import User

# ── Plan mapping ──────────────────────────────────────────────────────────────
# Frontend plan ids: free | pro | premium — backend PlanName: free | pro | pro_plus

FE_TO_BE_PLAN: dict[str, PlanName] = {
    "free": PlanName.free,
    "pro": PlanName.pro,
    "premium": PlanName.pro_plus,
}
BE_TO_FE_PLAN: dict[PlanName, str] = {v: k for k, v in FE_TO_BE_PLAN.items()}

PLAN_PRICE_LABELS: dict[str, str] = {
    "free": "$0 / month",
    "pro": "$49 / month",
    "premium": "$199 / month",
}

PLAN_DISPLAY_NAMES: dict[str, str] = {"free": "Free", "pro": "Pro", "premium": "Premium"}

# Display ceilings for quotas stored as -1 (unlimited) so the frontend's
# "used / total" progress bars always have a finite denominator.
PLAN_DISPLAY_LIMITS: dict[str, dict[str, int]] = {
    "free": {"messages": 50, "docs": 10, "urls": 5, "chunks": 1000},
    "pro": {"messages": 10000, "docs": 100, "urls": 50, "chunks": 5000},
    "premium": {"messages": 50000, "docs": 500, "urls": 200, "chunks": 20000},
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

def fe_plan_for_subscription(subscription: Optional[Subscription]) -> str:
    if subscription is None:
        return "free"
    return BE_TO_FE_PLAN.get(subscription.plan_name, "free")

def onboarding_completed(subscription: Optional[Subscription]) -> bool:
    return subscription is not None
