"""
Role-based access control.

`get_current_role` resolves the caller's role within their tenant from the
tenant_users membership table. The product is one-user-per-tenant today, so a
user with no membership row is treated as the tenant `owner` (the historical
behaviour — every existing user owns their workspace). `require_role(...)`
builds a FastAPI dependency that admits only the listed roles.

Roles are resolved server-side per request (not baked into the JWT) so a role
change takes effect immediately rather than only when a new token is minted —
important given the 15-second user cache.
"""

from __future__ import annotations

from typing import Iterable

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.tenant_user import TenantRole, TenantUser
from app.models.user import User
from app.services.auth_service import get_current_user


async def get_current_role(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TenantRole:
    result = await db.execute(
        select(TenantUser.role).where(
            TenantUser.tenant_id == current_user.tenant_id,
            TenantUser.user_id == current_user.id,
        )
    )
    role = result.scalar_one_or_none()
    # No membership row → the sole user of a single-user tenant → owner.
    return role or TenantRole.owner


def require_role(*allowed: TenantRole):
    """Return a dependency that admits only callers whose role is in `allowed`.
    Returns the user so it can replace Depends(get_current_user)."""
    allowed_set = set(allowed)

    async def _dep(
        current_user: User = Depends(get_current_user),
        role: TenantRole = Depends(get_current_role),
    ) -> User:
        if role not in allowed_set:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to perform this action.",
            )
        return current_user

    return _dep


# Common gates, named for readability at call sites.
require_owner = require_role(TenantRole.owner)
require_admin = require_role(TenantRole.owner, TenantRole.admin)
