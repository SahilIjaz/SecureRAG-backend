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


from fastapi import Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.tenant_user import TenantRole, TenantUser
from app.models.user import User
from app.services.auth_service import get_any_valid_user, get_current_user


async def resolve_role(user: User, db: AsyncSession) -> TenantRole:
    """The user's role in their tenant, resolved from tenant_users.

    A missing membership row is only treated as `owner` for the historical
    single-user case — i.e. when the tenant has NO membership rows at all.
    If the tenant already has members and this user isn't one of them, they
    get the least-privileged role instead of silently becoming an owner
    (fail closed)."""
    result = await db.execute(
        select(TenantUser.role).where(
            TenantUser.tenant_id == user.tenant_id,
            TenantUser.user_id == user.id,
        )
    )
    role = result.scalar_one_or_none()
    if role is not None:
        return role
    rows = await db.execute(
        select(func.count()).select_from(TenantUser).where(TenantUser.tenant_id == user.tenant_id)
    )
    return TenantRole.owner if rows.scalar_one() == 0 else TenantRole.agent


async def get_current_role(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TenantRole:
    return await resolve_role(current_user, db)


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


async def require_owner_any_valid(
    current_user: User = Depends(get_any_valid_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Owner gate for endpoints that must also work with an onboarding token
    (the onboarding wizard). Only the workspace owner may configure the
    workspace — an invited admin/agent can never drive these endpoints."""
    if await resolve_role(current_user, db) != TenantRole.owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the workspace owner can do this.",
        )
    return current_user
