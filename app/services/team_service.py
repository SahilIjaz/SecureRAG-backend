"""
Team / RBAC service: inviting agents into a tenant and accepting invites.

The owner invites an agent by email; a one-time link is mailed. Only the bcrypt
hash of the token is stored. Accepting the link creates the agent's User in the
owner's tenant (email pre-verified — the click proved mailbox control) and a
tenant_users row with role=agent, in a single transaction. Agent counts are
capped by the plan's max_agents entitlement.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.email import send_email
from app.core.entitlements import get_entitlements
from app.core.security import hash_password, verify_password
from app.models.invite import Invite
from app.models.subscription import Subscription
from app.models.tenant import Tenant
from app.models.tenant_user import TenantRole, TenantUser
from app.models.user import User

logger = logging.getLogger(__name__)

INVITE_TTL_HOURS = 48


async def count_active_agents(tenant_id, db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count()).select_from(TenantUser).where(
            TenantUser.tenant_id == tenant_id,
            TenantUser.role == TenantRole.agent,
        )
    )
    return result.scalar_one()


async def count_pending_invites(tenant_id, db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count()).select_from(Invite).where(
            Invite.tenant_id == tenant_id,
            Invite.accepted_at.is_(None),
            Invite.revoked_at.is_(None),
        )
    )
    return result.scalar_one()


async def _entitlements_for_tenant(tenant_id, db: AsyncSession):
    result = await db.execute(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    )
    return get_entitlements(result.scalar_one_or_none())


async def create_invite(
    tenant_id, email: str, created_by_user_id, db: AsyncSession
) -> str:
    """Create a pending agent invite and email the link. Returns the raw token
    (also embedded in the emailed link). Enforces the plan's agent cap counting
    active agents + still-pending invites."""
    email = email.lower().strip()

    ent = await _entitlements_for_tenant(tenant_id, db)
    if ent.max_agents <= 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your plan doesn't include team agents. Upgrade to invite teammates.",
        )
    used = await count_active_agents(tenant_id, db) + await count_pending_invites(tenant_id, db)
    if used >= ent.max_agents:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Agent limit reached. Your plan allows {ent.max_agents} agents.",
        )

    # A user (on any tenant) already owns this email — the model is one
    # workspace per user, so they can't also be an agent elsewhere.
    existing_user = await db.execute(select(User.id).where(User.email == email))
    if existing_user.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Someone with this email already has an account.",
        )

    # Supersede any prior pending invite to the same email for this tenant.
    prior = await db.execute(
        select(Invite).where(
            Invite.tenant_id == tenant_id,
            Invite.email == email,
            Invite.accepted_at.is_(None),
            Invite.revoked_at.is_(None),
        )
    )
    for old in prior.scalars().all():
        old.revoked_at = datetime.now(timezone.utc)

    raw_token = secrets.token_urlsafe(32)
    invite = Invite(
        tenant_id=tenant_id,
        email=email,
        role=TenantRole.agent,
        token_hash=hash_password(raw_token),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=INVITE_TTL_HOURS),
        created_by_user_id=created_by_user_id,
    )
    db.add(invite)
    await db.flush()

    link = f"{settings.FRONTEND_URL.rstrip('/')}/join?token={raw_token}"
    try:
        await send_email(
            recipient_email=email,
            subject="You've been invited to join a Nexus workspace",
            html_content=(
                f"<p>You've been invited to join a workspace on Nexus as a support agent.</p>"
                f'<p><a href="{link}">Accept the invitation</a> (link expires in {INVITE_TTL_HOURS} hours).</p>'
            ),
            text_content=f"You've been invited to join a Nexus workspace. Accept here: {link}",
        )
    except Exception:
        logger.exception("Failed to send invite email to %s", email)
    return raw_token


async def revoke_invite(invite_id, tenant_id, db: AsyncSession) -> None:
    result = await db.execute(
        select(Invite).where(Invite.id == invite_id, Invite.tenant_id == tenant_id)
    )
    invite = result.scalar_one_or_none()
    if invite is None or not invite.is_pending:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found.")
    invite.revoked_at = datetime.now(timezone.utc)
    await db.flush()


async def accept_invite(
    raw_token: str, full_name: str, password: str, db: AsyncSession
) -> User:
    """Atomically consume a valid invite and create the agent user. The invite's
    token is looked up by verifying the hash against pending, unexpired rows."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Invite).where(
            Invite.accepted_at.is_(None),
            Invite.revoked_at.is_(None),
            Invite.expires_at > now,
        )
    )
    candidates = result.scalars().all()
    invite = next((i for i in candidates if verify_password(raw_token, i.token_hash)), None)
    if invite is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This invitation link is invalid or has expired.",
        )

    # Guard against a race where the email was claimed between invite and accept.
    existing = await db.execute(select(User.id).where(User.email == invite.email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    user = User(
        tenant_id=invite.tenant_id,
        full_name=(full_name or "").strip() or invite.email.split("@")[0],
        email=invite.email,
        password_hash=hash_password(password),
        is_email_verified=True,  # the emailed link is the verification
    )
    db.add(user)
    await db.flush()
    db.add(TenantUser(tenant_id=invite.tenant_id, user_id=user.id, role=invite.role))
    invite.accepted_at = now
    await db.flush()
    return user
