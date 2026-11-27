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

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.email import send_email
from app.core.entitlements import get_entitlements
from app.core.security import hash_password, verify_password
from app.models.invite import Invite
from app.models.subscription import Subscription
from app.models.tenant_user import TenantRole, TenantUser
from app.models.user import User

logger = logging.getLogger(__name__)

INVITE_TTL_HOURS = 48


async def count_active_agents(tenant_id, db: AsyncSession) -> int:
    """Seats in use = every non-owner member (admins and agents share the
    plan's team-seat cap, so promoting someone can't bypass it)."""
    result = await db.execute(
        select(func.count()).select_from(TenantUser).where(
            TenantUser.tenant_id == tenant_id,
            TenantUser.role != TenantRole.owner,
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
    tenant_id, email: str, created_by_user_id, db: AsyncSession,
    role: TenantRole = TenantRole.agent,
) -> str:
    """Create a pending agent invite and email the link. Returns the raw token
    (also embedded in the emailed link). Enforces the plan's agent cap counting
    active agents + still-pending invites."""
    email = email.lower().strip()
    # Defence in depth: the endpoint's schema already restricts this, but the
    # service must never mint an owner invite regardless of caller.
    if role not in (TenantRole.admin, TenantRole.agent):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invites can only grant the admin or agent role.",
        )

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
        role=role,
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
                f"<p>You've been invited to join a workspace on Nexus as {'an admin' if role == TenantRole.admin else 'a support agent'}.</p>"
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


async def change_member_role(
    tenant_id, actor_user_id, target_user_id, new_role: TenantRole, db: AsyncSession
) -> TenantUser:
    """Owner-only (enforced by the endpoint). Switch a non-owner member between
    admin and agent. The owner's role is immutable here, nobody can change
    their own role, and ownership can never be granted."""
    from app.services.auth_service import invalidate_user_cache

    if new_role not in (TenantRole.admin, TenantRole.agent):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Members can only be set to admin or agent.",
        )
    if target_user_id == actor_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You can't change your own role.",
        )
    result = await db.execute(
        select(TenantUser).where(
            TenantUser.tenant_id == tenant_id, TenantUser.user_id == target_user_id
        ).with_for_update()
    )
    tu = result.scalar_one_or_none()
    if tu is None or tu.role == TenantRole.owner:
        # Not a member of this workspace, or the owner — indistinguishable on purpose.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found.")
    if tu.role == new_role:
        return tu

    tu.role = new_role
    user = (await db.execute(select(User).where(User.id == target_user_id))).scalar_one_or_none()
    if user is not None:
        # Role is resolved per request so the change is already live; bumping
        # token_version also ends their current sessions, so a demoted admin
        # can't keep a cached admin UI open and the client re-reads its role.
        user.token_version = (user.token_version or 0) + 1
        invalidate_user_cache(user.id)
    await db.flush()
    return tu
