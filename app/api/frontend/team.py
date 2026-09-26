"""
Team dashboard — GET /api/team/stats (owner-only).

Per-member support stats derived from data the queue slice already records:
chats handled (assigned_user_id), chats resolved (resolved_by_user_id),
currently-open live chats, and online status. No new columns needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.rbac import require_owner
from app.database import get_db
from app.models.conversation import Conversation
from app.models.tenant_user import TenantUser
from app.models.user import User

router = APIRouter(prefix="/team", tags=["Frontend — Team"])

class FEAgentStats(BaseModel):
    userId: str
    name: str
    email: str
    role: str
    online: bool
    handledCount: int
    resolvedCount: int
    openLiveCount: int

class FETeamStatsResponse(BaseModel):
    agents: List[FEAgentStats]

def _online(last_seen: Optional[datetime]) -> bool:
    if last_seen is None:
        return False
    seen = last_seen if last_seen.tzinfo else last_seen.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - seen) < timedelta(
        seconds=settings.PRESENCE_ONLINE_WINDOW_SECONDS
    )

@router.get("/stats", response_model=FETeamStatsResponse)
async def team_stats(
    current_user: User = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> FETeamStatsResponse:
    tenant_id = current_user.tenant_id

    # Members with their role + presence.
    members = (await db.execute(
        select(TenantUser.role, TenantUser.last_seen_at, User.id, User.full_name, User.email)
        .join(User, User.id == TenantUser.user_id)
        .where(TenantUser.tenant_id == tenant_id)
    )).all()

    # Per-user aggregate counts, computed in single grouped queries.
    handled = dict((uid, n) for uid, n in (await db.execute(
        select(Conversation.assigned_user_id, func.count())
        .where(Conversation.tenant_id == tenant_id, Conversation.assigned_user_id.isnot(None))
        .group_by(Conversation.assigned_user_id)
    )).all())
    resolved = dict((uid, n) for uid, n in (await db.execute(
        select(Conversation.resolved_by_user_id, func.count())
        .where(Conversation.tenant_id == tenant_id, Conversation.resolved_by_user_id.isnot(None))
        .group_by(Conversation.resolved_by_user_id)
    )).all())
    open_live = dict((uid, n) for uid, n in (await db.execute(
        select(Conversation.assigned_user_id, func.count())
        .where(
            Conversation.tenant_id == tenant_id,
            Conversation.is_live == True,  # noqa: E712
            Conversation.assigned_user_id.isnot(None),
        )
        .group_by(Conversation.assigned_user_id)
    )).all())

    agents = [
        FEAgentStats(
            userId=str(uid),
            name=name or "",
            email=email,
            role=role.value,
            online=_online(last_seen),
            handledCount=handled.get(uid, 0),
            resolvedCount=resolved.get(uid, 0),
            openLiveCount=open_live.get(uid, 0),
        )
        for role, last_seen, uid, name, email in members
    ]
    return FETeamStatsResponse(agents=agents)
