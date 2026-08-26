"""
Frontend-compat dashboard endpoints (/api/dashboard/...).

All eight widgets on the Overview page:
  stats, volume, sentiment, unresolved, gaps, topics, recent, usage

Metrics are aggregated from the conversations tables. A brand-new workspace
with no conversations gets zeros/empty lists — the frontend renders empty
states for those.
"""

import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, List, TypeVar

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.frontend import helpers
from app.database import get_db
from app.models.conversation import Conversation
from app.models.document import DocumentSource
from app.models.user import User
from app.schemas.frontend import (
    FEConversationTopic,
    FEKnowledgeGap,
    FEOverviewStats,
    FEPlanUsage,
    FERecentConversation,
    FESentimentData,
    FEUnresolvedQuestion,
    FEVolumePoint,
)
from app.services.auth_service import get_current_user

router = APIRouter(prefix="/dashboard", tags=["Frontend — Dashboard"])

WINDOW_DAYS = 30

# Every widget here recomputes its aggregate from scratch on every request —
# there's no external cache (Redis) in this app, and each of these round
# trips a remote Neon Postgres instance. A short in-process TTL cache (same
# pattern as helpers._widget_tenant_cache) cuts repeat DB load for a page
# that fires 8 of these on every mount, without the data going staler than
# the frontend's own React Query staleTime (60s — see dashboard.api.ts)
# already tolerates.
DASHBOARD_CACHE_TTL_SECONDS = 20
_T = TypeVar("_T")
_dashboard_cache: dict[tuple, tuple[float, object]] = {}

async def _cached(key: tuple, loader: Callable[[], Awaitable[_T]]) -> _T:
    now = time.monotonic()
    cached = _dashboard_cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]  # type: ignore[return-value]
    value = await loader()
    _dashboard_cache[key] = (now + DASHBOARD_CACHE_TTL_SECONDS, value)
    return value

def _now() -> datetime:
    return datetime.now(timezone.utc)

async def _conversations_since(
    tenant_id, since: datetime, db: AsyncSession, with_messages: bool = False
) -> List[Conversation]:
    query = select(Conversation).where(
        Conversation.tenant_id == tenant_id,
        Conversation.deleted_at.is_(None),
        Conversation.created_at >= since,
    )
    if with_messages:
        query = query.options(selectinload(Conversation.messages))
    result = await db.execute(query.order_by(Conversation.created_at.desc()))
    return list(result.scalars().all())

def _pct_change(current: float, previous: float) -> float:
    if previous == 0:
        return 100.0 if current > 0 else 0.0
    return round((current - previous) / previous * 100, 1)

def _avg_response_seconds(conversations: List[Conversation]) -> float:
    """Mean delay between a user message and the next bot/agent reply."""
    gaps: List[float] = []
    for convo in conversations:
        pending_user_ts = None
        for msg in convo.messages:
            if msg.role == "user":
                pending_user_ts = msg.created_at
            elif pending_user_ts is not None:
                gaps.append((msg.created_at - pending_user_ts).total_seconds())
                pending_user_ts = None
    if not gaps:
        return 0.0
    return round(sum(gaps) / len(gaps), 1)

@router.get("/stats", response_model=FEOverviewStats)
async def get_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEOverviewStats:
    async def _load() -> FEOverviewStats:
        now = _now()
        # A single query over the wider (2x) window already contains every conversation the
        # narrower "current" window would return, since WINDOW_DAYS*2 > WINDOW_DAYS — so split
        # it in Python instead of paying a second round trip for a strict subset of this data.
        cutoff = now - timedelta(days=WINDOW_DAYS)
        both_windows = await _conversations_since(
            current_user.tenant_id, now - timedelta(days=WINDOW_DAYS * 2), db, with_messages=True
        )
        current = [c for c in both_windows if c.created_at >= cutoff]
        previous = [c for c in both_windows if c.created_at < cutoff]

        def resolution_rate(convos: List[Conversation]) -> float:
            if not convos:
                return 0.0
            resolved = sum(1 for c in convos if c.status == "Resolved")
            return round(resolved / len(convos) * 100, 1)

        cur_unresolved = sum(1 for c in current if c.status == "Open")
        prev_unresolved = sum(1 for c in previous if c.status == "Open")
        cur_avg = _avg_response_seconds(current)
        prev_avg = _avg_response_seconds(previous)

        return FEOverviewStats(
            totalConversations=len(current),
            totalConversationsDelta=_pct_change(len(current), len(previous)),
            resolutionRate=resolution_rate(current),
            resolutionRateDelta=round(resolution_rate(current) - resolution_rate(previous), 1),
            unresolved=cur_unresolved,
            unresolvedDelta=cur_unresolved - prev_unresolved,
            avgResponseSeconds=cur_avg,
            avgResponseDelta=round(cur_avg - prev_avg, 1),
        )

    return await _cached(("stats", current_user.tenant_id), _load)

@router.get("/volume", response_model=List[FEVolumePoint])
async def get_volume(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[FEVolumePoint]:
    async def _load() -> List[FEVolumePoint]:
        now = _now()
        days = 14
        conversations = await _conversations_since(
            current_user.tenant_id, now - timedelta(days=days), db
        )
        counts = Counter(helpers._as_aware(c.created_at).date() for c in conversations)
        points: List[FEVolumePoint] = []
        for i in range(days - 1, -1, -1):
            day = (now - timedelta(days=i)).date()
            label = f"{day.strftime('%b')} {day.day}"
            points.append(FEVolumePoint(date=label, count=counts.get(day, 0)))
        return points

    return await _cached(("volume", current_user.tenant_id), _load)

@router.get("/sentiment", response_model=FESentimentData)
async def get_sentiment(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FESentimentData:
    async def _load() -> FESentimentData:
        conversations = await _conversations_since(
            current_user.tenant_id, _now() - timedelta(days=WINDOW_DAYS), db
        )
        total = len(conversations)
        if total == 0:
            return FESentimentData(positive=0, neutral=0, negative=0, csatScore=0.0)

        pos = sum(1 for c in conversations if c.sentiment == "Positive")
        neg = sum(1 for c in conversations if c.sentiment == "Negative")
        positive = round(pos / total * 100)
        negative = round(neg / total * 100)
        neutral = 100 - positive - negative
        # CSAT proxy on a 1–5 scale: 3 is neutral, shifted by the positive/negative balance.
        csat = round(min(5.0, max(1.0, 3 + 2 * (pos - neg) / total)), 1)
        return FESentimentData(positive=positive, neutral=neutral, negative=negative, csatScore=csat)

    return await _cached(("sentiment", current_user.tenant_id), _load)

@router.get("/unresolved", response_model=List[FEUnresolvedQuestion])
async def get_unresolved(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[FEUnresolvedQuestion]:
    async def _load() -> List[FEUnresolvedQuestion]:
        # Only the 5 most recent open conversations are ever shown, so filter
        # status and cap the row count in SQL instead of pulling every open-or-
        # not conversation from a 90-day window (with all of its messages
        # eagerly loaded) and slicing to 5 in Python.
        result = await db.execute(
            select(Conversation)
            .where(
                Conversation.tenant_id == current_user.tenant_id,
                Conversation.deleted_at.is_(None),
                Conversation.status == "Open",
                Conversation.created_at >= _now() - timedelta(days=WINDOW_DAYS * 3),
            )
            .options(selectinload(Conversation.messages))
            .order_by(Conversation.created_at.desc())
            .limit(5)
        )
        open_convos = list(result.scalars().all())

        items: List[FEUnresolvedQuestion] = []
        for convo in open_convos:
            first_user_msg = next((m.text for m in convo.messages if m.role == "user"), "")
            question = first_user_msg if len(first_user_msg) <= 60 else first_user_msg[:57] + "..."
            items.append(
                FEUnresolvedQuestion(
                    id=str(convo.id),
                    question=question or "(no message)",
                    askedCount=1,
                    reason=convo.unresolved_reason or "No matching doc",
                    lastAsked=helpers.time_ago(convo.created_at),
                )
            )
        return items

    return await _cached(("unresolved", current_user.tenant_id), _load)

def _priority_for(count: int) -> str:
    if count >= 10:
        return "High"
    if count >= 5:
        return "Medium"
    return "Low"

@router.get("/gaps", response_model=List[FEKnowledgeGap])
async def get_gaps(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[FEKnowledgeGap]:
    async def _load() -> List[FEKnowledgeGap]:
        # The topic tally needs every open conversation in the window (it's a
        # genuine full-window aggregate, not a top-N list), but "open" and
        # "not deleted" can still be pushed into the WHERE clause instead of
        # fetching every status over 90 days and filtering in Python — and a
        # bare topic column is enough here, so skip hydrating full
        # Conversation entities (visitor info, sentiment, etc.) entirely.
        result = await db.execute(
            select(Conversation.topic).where(
                Conversation.tenant_id == current_user.tenant_id,
                Conversation.deleted_at.is_(None),
                Conversation.status == "Open",
                Conversation.created_at >= _now() - timedelta(days=WINDOW_DAYS * 3),
            )
        )
        open_topics = Counter((topic or "General") for topic in result.scalars().all())
        return [
            FEKnowledgeGap(id=str(i + 1), topic=topic, queryCount=count, priority=_priority_for(count))
            for i, (topic, count) in enumerate(open_topics.most_common(5))
        ]

    return await _cached(("gaps", current_user.tenant_id), _load)

@router.get("/topics", response_model=List[FEConversationTopic])
async def get_topics(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[FEConversationTopic]:
    async def _load() -> List[FEConversationTopic]:
        conversations = await _conversations_since(
            current_user.tenant_id, _now() - timedelta(days=WINDOW_DAYS), db
        )
        total = len(conversations)
        if total == 0:
            return []
        topic_counts = Counter((c.topic or "Other") for c in conversations)
        return [
            FEConversationTopic(label=topic, percentage=round(count / total * 100))
            for topic, count in topic_counts.most_common(5)
        ]

    return await _cached(("topics", current_user.tenant_id), _load)

@router.get("/recent", response_model=List[FERecentConversation])
async def get_recent(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[FERecentConversation]:
    result = await db.execute(
        select(Conversation)
        .where(
            Conversation.tenant_id == current_user.tenant_id,
            Conversation.deleted_at.is_(None),
        )
        .options(selectinload(Conversation.messages))
        .order_by(Conversation.created_at.desc())
        .limit(5)
    )
    conversations = list(result.scalars().all())

    items: List[FERecentConversation] = []
    for convo in conversations:
        first_user_msg = next((m.text for m in convo.messages if m.role == "user"), "")
        preview = first_user_msg if len(first_user_msg) <= 40 else first_user_msg[:37] + "..."
        items.append(
            FERecentConversation(
                id=str(convo.id),
                initials=helpers.initials(convo.visitor_name),
                name=convo.visitor_name,
                preview=preview,
                timeAgo=helpers.time_ago(convo.created_at).replace(" ago", ""),
                status=convo.status,
                sentiment=convo.sentiment,
            )
        )
    return items

@router.get("/usage", response_model=FEPlanUsage)
async def get_usage(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEPlanUsage:
    _tenant, subscription = await helpers.get_tenant_and_subscription(current_user, db)
    quota, usage = await helpers.get_quota_and_usage(current_user.tenant_id, db)
    # Aggregate counts by source instead of get_active_documents()'s full-row
    # fetch — the usage bars only need len() per source, not the documents.
    doc_counts = await helpers.get_active_document_counts(current_user.tenant_id, db)
    url_count = doc_counts.get(DocumentSource.scraped, 0)
    faq_count = doc_counts.get(DocumentSource.faq, 0)
    file_count = sum(
        count for source, count in doc_counts.items()
        if source not in (DocumentSource.scraped, DocumentSource.faq)
    )

    fe_plan = helpers.fe_plan_for_subscription(subscription)
    display = helpers.PLAN_DISPLAY_LIMITS[fe_plan]

    messages_total = display["messages"]
    if quota and quota.max_questions_per_month != -1:
        messages_total = quota.max_questions_per_month
    docs_total = display["docs"]
    if quota and quota.max_documents != -1:
        docs_total = quota.max_documents

    return FEPlanUsage(
        plan=helpers.PLAN_DISPLAY_NAMES[fe_plan],
        messagesUsed=usage.questions_used if usage else 0,
        messagesTotal=messages_total,
        # FAQ entries share the same document quota as files/URLs, so they
        # count toward the displayed usage too.
        docsUsed=file_count + faq_count,
        docsTotal=docs_total,
        urlsUsed=url_count,
        urlsTotal=display["urls"],
        resetsOn=helpers.format_date(helpers.first_of_next_month()),
    )
