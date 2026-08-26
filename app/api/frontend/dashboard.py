"""
Frontend-compat dashboard endpoints (/api/dashboard/...).

All eight widgets on the Overview page:
  stats, volume, sentiment, unresolved, gaps, topics, recent, usage

Metrics are aggregated from the conversations tables. A brand-new workspace
with no conversations gets zeros/empty lists — the frontend renders empty
states for those.
"""

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, Depends

from app.core.rbac import require_admin
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.frontend import helpers
from app.database import get_db
from app.models.conversation import Conversation
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
from app.services.classification_service import UNCLASSIFIED_TOPIC

# Dashboard analytics are owner/admin territory — agents see the conversation
# inbox, not workspace-wide metrics.
router = APIRouter(
    prefix="/dashboard",
    tags=["Frontend — Dashboard"],
    dependencies=[Depends(require_admin)],
)

WINDOW_DAYS = 30

def _now() -> datetime:
    return datetime.now(timezone.utc)

async def _conversations_since(
    tenant_id, since: datetime, db: AsyncSession, with_messages: bool = False
) -> List[Conversation]:
    query = select(Conversation).where(
        Conversation.tenant_id == tenant_id,
        Conversation.created_at >= since,
    )
    if with_messages:
        query = query.options(selectinload(Conversation.messages))
    result = await db.execute(query.order_by(Conversation.created_at.desc()))
    return list(result.scalars().all())

def _is_unresolved(convo: Conversation) -> bool:
    """
    Anything not Resolved still needs attention — including "Handed off", where
    the bot failed and escalated. Counting only "Open" left genuinely
    unanswered questions out of the gaps and unresolved cards entirely.
    """
    return convo.status != "Resolved"

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

    cur_unresolved = sum(1 for c in current if _is_unresolved(c))
    prev_unresolved = sum(1 for c in previous if _is_unresolved(c))
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

@router.get("/volume", response_model=List[FEVolumePoint])
async def get_volume(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[FEVolumePoint]:
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

@router.get("/sentiment", response_model=FESentimentData)
async def get_sentiment(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FESentimentData:
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

def _question_tokens(text: str) -> set:
    """Content words of a question, for similarity comparison."""
    import re

    cleaned = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    # Crude singular/plural fold so "emails"/"email" match.
    return {
        w[:-1] if len(w) > 3 and w.endswith("s") else w
        for w in cleaned.split()
        if w not in _STOPWORDS
    }

def _similar(a: set, b: set, threshold: float = 0.5) -> bool:
    """Jaccard overlap — merges verbatim repeats and close rewordings."""
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= threshold

_STOPWORDS = {
    "a", "an", "the", "is", "are", "do", "does", "did", "can", "could", "would",
    "i", "we", "you", "my", "our", "your", "to", "of", "for", "on", "in", "it",
    "how", "what", "where", "when", "why", "please", "hi", "hello", "there",
}

@router.get("/unresolved", response_model=List[FEUnresolvedQuestion])
async def get_unresolved(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[FEUnresolvedQuestion]:
    """
    Unresolved questions, grouped so repeats are counted rather than listed
    separately — ten visitors asking the same thing is one row with
    askedCount 10, which is what makes the card actionable.
    """
    conversations = await _conversations_since(
        current_user.tenant_id, _now() - timedelta(days=WINDOW_DAYS * 3), db, with_messages=True
    )
    open_convos = [c for c in conversations if _is_unresolved(c)]

    # Cluster by topic + token similarity, so the same question asked by five
    # visitors is one row with askedCount 5 rather than five rows of 1.
    groups: list = []
    for convo in open_convos:
        first_user_msg = next((m.text for m in convo.messages if m.role == "user"), "")
        if not first_user_msg:
            continue
        tokens = _question_tokens(first_user_msg)
        created = helpers._as_aware(convo.created_at)
        topic = convo.topic or ""

        match = next(
            (g for g in groups if g["topic"] == topic and _similar(g["tokens"], tokens)),
            None,
        )
        if match is None:
            groups.append({
                "id": str(convo.id),
                "topic": topic,
                "tokens": tokens,
                "question": first_user_msg,
                "count": 1,
                "reason": convo.unresolved_reason or "No matching doc",
                "last_at": created,
            })
        else:
            match["count"] += 1
            match["tokens"] |= tokens
            if created > match["last_at"]:
                match["last_at"] = created
                match["question"] = first_user_msg
                match["id"] = str(convo.id)
                match["reason"] = convo.unresolved_reason or match["reason"]

    ranked = sorted(groups, key=lambda g: (-g["count"], -g["last_at"].timestamp()))
    return [
        FEUnresolvedQuestion(
            id=g["id"],
            question=g["question"] if len(g["question"]) <= 60 else g["question"][:57] + "...",
            askedCount=g["count"],
            reason=g["reason"],
            lastAsked=helpers.time_ago(g["last_at"]),
        )
        for g in ranked[:5]
    ]

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
    conversations = await _conversations_since(
        current_user.tenant_id, _now() - timedelta(days=WINDOW_DAYS * 3), db
    )
    open_topics = Counter(
        (c.topic or UNCLASSIFIED_TOPIC) for c in conversations if _is_unresolved(c)
    )
    gaps = [
        FEKnowledgeGap(id=str(i + 1), topic=topic, queryCount=count, priority=_priority_for(count))
        for i, (topic, count) in enumerate(open_topics.most_common(5))
    ]
    return gaps

@router.get("/topics", response_model=List[FEConversationTopic])
async def get_topics(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[FEConversationTopic]:
    conversations = await _conversations_since(
        current_user.tenant_id, _now() - timedelta(days=WINDOW_DAYS), db
    )
    total = len(conversations)
    if total == 0:
        return []
    topic_counts = Counter((c.topic or UNCLASSIFIED_TOPIC) for c in conversations)
    return [
        FEConversationTopic(label=topic, percentage=round(count / total * 100))
        for topic, count in topic_counts.most_common(5)
    ]

@router.get("/recent", response_model=List[FERecentConversation])
async def get_recent(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[FERecentConversation]:
    result = await db.execute(
        select(Conversation)
        .where(Conversation.tenant_id == current_user.tenant_id)
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
    docs = await helpers.get_active_documents(current_user.tenant_id, db)
    file_docs, url_docs, faq_docs = helpers.split_docs_and_urls(docs)

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
        docsUsed=len(file_docs) + len(faq_docs),
        docsTotal=docs_total,
        urlsUsed=len(url_docs),
        urlsTotal=display["urls"],
        storageUsedMb=round((usage.storage_used_mb if usage else 0.0) or 0.0, 2),
        storageTotalMb=float(quota.max_storage_mb) if quota else 0.0,
        resetsOn=helpers.format_date(helpers.first_of_next_month()),
    )
