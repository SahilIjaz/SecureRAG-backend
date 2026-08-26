"""
Owner presence — /api/presence/ping.

The dashboard calls this every ~20s while open (Frontend/src/api/presence.api.ts)
so the system has a rough, honest answer to "is the owner around right now"
for the live-agent-handoff flow (NexusContext/LIVE_AGENT_HANDOFF_PLAN.md §2).
Presence is a hint, not a promise — see helpers.is_owner_currently_online for
how it's actually computed from this ping's timestamp.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.tenant import Tenant
from app.models.tenant_user import TenantUser
from app.models.user import User
from app.services.auth_service import get_current_user

router = APIRouter(prefix="/presence", tags=["Frontend — Presence"])

@router.post("/ping", status_code=204)
async def presence_ping(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    # Per-user presence: stamp THIS member's membership row (multi-agent). The
    # legacy tenant-level owner columns are kept in sync during the transition
    # so anything still reading them keeps working.
    # func.now() (DB server clock), not app-server time — avoids clock skew
    # between wherever the API process runs and the database.
    await db.execute(
        update(TenantUser)
        .where(
            TenantUser.tenant_id == current_user.tenant_id,
            TenantUser.user_id == current_user.id,
        )
        .values(last_seen_at=func.now())
    )
    await db.execute(
        update(Tenant)
        .where(Tenant.id == current_user.tenant_id)
        .values(is_owner_online=True, last_seen_at=func.now())
    )
    await db.flush()
