"""
Frontend-compat settings endpoints (/api/settings/...).

  Workspace:      GET/PUT/DELETE /api/settings/workspace
  Notifications:  GET/PUT        /api/settings/notifications   (PUT takes a partial patch)
  Billing:        GET            /api/settings/billing
                  POST           /api/settings/billing/checkout        (returns Stripe SetupIntent clientSecret)
                  POST           /api/settings/billing/cancel
                  PUT            /api/settings/billing/plan             (existing subscription only — no card)
                  POST           /api/settings/billing/payment-method/setup-intent
  API keys:       GET/POST       /api/settings/api-keys
                  DELETE         /api/settings/api-keys/{id}
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.config import settings
from app.core.rate_limit import limiter
from app.core.rbac import require_owner
from sqlalchemy import select, update as _sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.frontend import helpers
from app.core.security import hash_password
from app.database import get_db
from app.models.subscription import SubscriptionStatus
from app.models.tenant import Tenant
from app.models.conversation import Conversation
from app.models.invite import Invite
from app.models.tenant_settings import ApiKey, NotificationSetting, PaymentMethod
from app.models.tenant_user import TenantRole, TenantUser
from app.models.user import User
from app.services import team_service
from app.services.auth_service import invalidate_user_cache
from app.schemas.frontend import (
    FEApiKey,
    FEBillingInfo,
    FECheckoutRequest,
    FECheckoutResponse,
    FEChangePlanRequest,
    FEAcceptInviteRequest,
    FECreateApiKeyRequest,
    FECreateApiKeyResponse,
    FEInviteRequest,
    FEMember,
    FEPendingInvite,
    FETeamRoster,
    FENotificationSettings,
    FENotificationSettingsPatch,
    FEPaymentMethod,
    FESaveWorkspaceSettingsResponse,
    FESetupIntentResponse,
    FESuccessResponse,
    FEWorkspaceSettings,
)
from app.services import stripe_service
from app.services.auth_service import _slugify, get_current_user

router = APIRouter(prefix="/settings", tags=["Frontend — Settings"])

# ── Workspace ─────────────────────────────────────────────────────────────────

def _fe_workspace(tenant: Tenant) -> FEWorkspaceSettings:
    return FEWorkspaceSettings(
        name=tenant.workspace_name if tenant.workspace_name != "__pending__" else "",
        urlSlug=tenant.slug,
        industry=tenant.business_category or "",
    )

@router.get("/workspace", response_model=FEWorkspaceSettings)
async def get_workspace(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEWorkspaceSettings:
    tenant = await helpers.get_tenant(current_user, db)
    return _fe_workspace(tenant)

@router.put("/workspace", response_model=FESaveWorkspaceSettingsResponse)
async def save_workspace(
    body: FEWorkspaceSettings,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FESaveWorkspaceSettingsResponse:
    tenant = await helpers.get_tenant(current_user, db)

    if body.name.strip():
        tenant.workspace_name = body.name.strip()
    if body.industry.strip():
        tenant.business_category = body.industry.strip()

    new_slug = _slugify(body.urlSlug.strip()) if body.urlSlug.strip() else tenant.slug
    if new_slug != tenant.slug:
        result = await db.execute(
            select(Tenant).where(Tenant.slug == new_slug, Tenant.id != tenant.id)
        )
        if result.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This workspace URL is already taken.",
            )
        tenant.slug = new_slug

    await db.flush()
    return FESaveWorkspaceSettingsResponse(success=True, settings=_fe_workspace(tenant))

@router.delete("/workspace", response_model=FESuccessResponse)
async def delete_workspace(
    current_user: User = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> FESuccessResponse:
    """Soft-delete: deactivates the tenant and the user so every later request is rejected."""
    tenant = await helpers.get_tenant(current_user, db)
    tenant.is_active = False
    current_user.is_active = False
    await db.flush()
    invalidate_user_cache(current_user.id)
    return FESuccessResponse()

# ── Notifications ─────────────────────────────────────────────────────────────

async def _get_or_create_notifications(user: User, db: AsyncSession) -> NotificationSetting:
    result = await db.execute(
        select(NotificationSetting).where(NotificationSetting.tenant_id == user.tenant_id)
    )
    record = result.scalar_one_or_none()
    if record is None:
        record = NotificationSetting(tenant_id=user.tenant_id)
        db.add(record)
        await db.flush()
    return record

def _fe_notifications(record: NotificationSetting) -> FENotificationSettings:
    return FENotificationSettings(
        unresolvedConversations=record.unresolved_conversations,
        weeklySummary=record.weekly_summary,
        knowledgeGapsDetected=record.knowledge_gaps_detected,
        planUsageWarnings=record.plan_usage_warnings,
        productUpdates=record.product_updates,
    )

@router.get("/notifications", response_model=FENotificationSettings)
async def get_notifications(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FENotificationSettings:
    record = await _get_or_create_notifications(current_user, db)
    return _fe_notifications(record)

@router.put("/notifications", response_model=FENotificationSettings)
async def update_notifications(
    body: FENotificationSettingsPatch,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FENotificationSettings:
    record = await _get_or_create_notifications(current_user, db)
    if body.unresolvedConversations is not None:
        record.unresolved_conversations = body.unresolvedConversations
    if body.weeklySummary is not None:
        record.weekly_summary = body.weeklySummary
    if body.knowledgeGapsDetected is not None:
        record.knowledge_gaps_detected = body.knowledgeGapsDetected
    if body.planUsageWarnings is not None:
        record.plan_usage_warnings = body.planUsageWarnings
    if body.productUpdates is not None:
        record.product_updates = body.productUpdates
    await db.flush()
    return _fe_notifications(record)

# ── Billing ───────────────────────────────────────────────────────────────────

_FE_STATUS: dict[SubscriptionStatus, str] = {
    SubscriptionStatus.active: "Active",
    SubscriptionStatus.trial: "Trialing",
    SubscriptionStatus.cancelled: "Canceled",
    SubscriptionStatus.expired: "Canceled",
}

async def _get_or_create_payment_method(user: User, db: AsyncSession) -> PaymentMethod:
    result = await db.execute(
        select(PaymentMethod).where(PaymentMethod.tenant_id == user.tenant_id)
    )
    record = result.scalar_one_or_none()
    if record is None:
        record = PaymentMethod(tenant_id=user.tenant_id)
        db.add(record)
        await db.flush()
    return record

async def _billing_info(user: User, db: AsyncSession) -> FEBillingInfo:
    subscription = await helpers.get_subscription(user.tenant_id, db)
    payment = await _get_or_create_payment_method(user, db)
    fe_plan = helpers.fe_plan_for_subscription(subscription)

    fe_status = _FE_STATUS.get(subscription.status, "Active") if subscription else "Active"

    renews_on = helpers.first_of_next_month()
    if subscription is not None and subscription.expires_at is not None:
        renews_on = subscription.expires_at

    trial_ends_on = None
    if subscription is not None and subscription.status == SubscriptionStatus.trial and subscription.expires_at:
        trial_ends_on = helpers.format_date(subscription.expires_at)

    return FEBillingInfo(
        planId=fe_plan,
        status=fe_status,
        priceLabel=helpers.PLAN_PRICE_LABELS[fe_plan],
        renewsOn=helpers.format_date(renews_on),
        paymentMethod=FEPaymentMethod(brand=payment.brand, last4=payment.last4, expiry=payment.expiry),
        trialEndsOn=trial_ends_on,
    )

@router.get("/billing", response_model=FEBillingInfo)
async def get_billing(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEBillingInfo:
    return await _billing_info(current_user, db)

@router.post("/billing/checkout", response_model=FECheckoutResponse)
@limiter.limit("10/minute")
async def start_checkout(
    request: Request,
    body: FECheckoutRequest,
    current_user: User = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> FECheckoutResponse:
    """Starts (or restarts) a 3-day trial on the chosen plan and returns a
    SetupIntent client secret for the frontend to collect the card via
    Stripe Elements. The local Subscription row is only updated with the
    Stripe IDs here — plan/status/quota land once the webhook confirms the
    trial actually started."""
    tenant = await helpers.get_tenant(current_user, db)
    subscription = await helpers.get_subscription(current_user.tenant_id, db)
    if subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No subscription found.")

    plan_name = helpers.FE_TO_BE_PLAN[body.planId]
    client_secret = await stripe_service.start_trial_subscription(
        tenant, current_user, subscription, plan_name, db
    )
    return FECheckoutResponse(clientSecret=client_secret)

@router.post("/billing/cancel", response_model=FEBillingInfo)
async def cancel_plan(
    current_user: User = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> FEBillingInfo:
    subscription = await helpers.get_subscription(current_user.tenant_id, db)
    if subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No subscription found.")
    await stripe_service.cancel_subscription(subscription, db)
    return await _billing_info(current_user, db)

@router.put("/billing/plan", response_model=FEBillingInfo)
async def change_plan(
    body: FEChangePlanRequest,
    current_user: User = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> FEBillingInfo:
    subscription = await helpers.get_subscription(current_user.tenant_id, db)
    if subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No subscription found.")
    if not subscription.stripe_subscription_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No active subscription to change — start checkout first.",
        )

    plan_name = helpers.FE_TO_BE_PLAN[body.planId]
    await stripe_service.change_subscription_plan(subscription, plan_name, db)
    return await _billing_info(current_user, db)

@router.post("/billing/payment-method/setup-intent", response_model=FESetupIntentResponse)
async def create_payment_method_setup_intent(
    current_user: User = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> FESetupIntentResponse:
    """Returns a SetupIntent client secret for updating the card on file.
    The actual brand/last4/expiry update happens via the
    setup_intent.succeeded webhook once Stripe confirms the new card."""
    subscription = await helpers.get_subscription(current_user.tenant_id, db)
    if subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No subscription found.")
    try:
        client_secret = await stripe_service.create_card_update_setup_intent(subscription)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    return FESetupIntentResponse(clientSecret=client_secret)

# ── API keys ──────────────────────────────────────────────────────────────────

@router.get("/api-keys", response_model=List[FEApiKey])
async def list_api_keys(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[FEApiKey]:
    result = await db.execute(
        select(ApiKey)
        .where(ApiKey.tenant_id == current_user.tenant_id, ApiKey.is_revoked == False)
        .order_by(ApiKey.created_at.desc())
    )
    return [
        FEApiKey(
            id=str(k.id),
            label=k.label,
            maskedKey=k.masked_key,
            createdLabel=f"Created {helpers.format_date(k.created_at)}",
        )
        for k in result.scalars().all()
    ]

@router.post("/api-keys", response_model=FECreateApiKeyResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def create_api_key(
    request: Request,
    body: FECreateApiKeyRequest,
    current_user: User = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> FECreateApiKeyResponse:
    raw_key = helpers.generate_workspace_api_key()
    loop = asyncio.get_event_loop()
    key_hash = await loop.run_in_executor(None, hash_password, raw_key)

    record = ApiKey(
        tenant_id=current_user.tenant_id,
        label=body.label.strip(),
        key_hash=key_hash,
        masked_key=helpers.mask_key(raw_key),
    )
    db.add(record)
    await db.flush()
    await db.refresh(record)

    return FECreateApiKeyResponse(
        rawKey=raw_key,
        key=FEApiKey(
            id=str(record.id),
            label=record.label,
            maskedKey=record.masked_key,
            createdLabel="Created just now",
        ),
    )

@router.delete("/api-keys/{key_id}", response_model=FESuccessResponse)
async def revoke_api_key(
    key_id: str,
    current_user: User = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> FESuccessResponse:
    try:
        kid = uuid.UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found.")

    result = await db.execute(
        select(ApiKey).where(ApiKey.id == kid, ApiKey.tenant_id == current_user.tenant_id)
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found.")

    record.is_revoked = True
    await db.flush()
    return FESuccessResponse()

# ── Team / RBAC ───────────────────────────────────────────────────────────────

def _member_is_online(last_seen_at) -> bool:
    if last_seen_at is None:
        return False
    seen = last_seen_at if last_seen_at.tzinfo else last_seen_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - seen) < timedelta(
        seconds=settings.PRESENCE_ONLINE_WINDOW_SECONDS
    )

@router.get("/members", response_model=FETeamRoster)
async def list_members(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FETeamRoster:
    """The workspace's team: members (with role + online state) and pending
    invites, plus the plan's agent quota. Readable by any member."""
    rows = await db.execute(
        select(TenantUser.role, TenantUser.last_seen_at, User.id, User.full_name, User.email)
        .join(User, User.id == TenantUser.user_id)
        .where(TenantUser.tenant_id == current_user.tenant_id)
    )
    members = [
        FEMember(
            id=str(uid), name=name or "", email=email, role=role.value,
            online=_member_is_online(last_seen),
        )
        for role, last_seen, uid, name, email in rows.all()
    ]
    if not members:
        # Pre-backfill fallback: treat the caller as owner.
        members = [FEMember(
            id=str(current_user.id), name=current_user.full_name or "",
            email=current_user.email, role="owner", online=True,
        )]

    invite_rows = await db.execute(
        select(Invite).where(
            Invite.tenant_id == current_user.tenant_id,
            Invite.accepted_at.is_(None),
            Invite.revoked_at.is_(None),
        ).order_by(Invite.created_at.desc())
    )
    pending = [
        FEPendingInvite(
            id=str(inv.id), email=inv.email, role=inv.role.value,
            invitedLabel=helpers.time_ago(inv.created_at),
        )
        for inv in invite_rows.scalars().all()
    ]

    ent = await team_service._entitlements_for_tenant(current_user.tenant_id, db)
    agents_used = await team_service.count_active_agents(current_user.tenant_id, db)
    return FETeamRoster(
        members=members, pendingInvites=pending,
        agentsUsed=agents_used, agentsLimit=ent.max_agents,
    )

@router.post("/members/invite", response_model=FEPendingInvite, status_code=status.HTTP_201_CREATED)
@limiter.limit("3/minute")
async def invite_member(
    request: Request,
    body: FEInviteRequest,
    current_user: User = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> FEPendingInvite:
    await team_service.create_invite(
        current_user.tenant_id, str(body.email), current_user.id, db
    )
    # Return the freshly-created pending invite for the UI.
    result = await db.execute(
        select(Invite).where(
            Invite.tenant_id == current_user.tenant_id,
            Invite.email == str(body.email).lower().strip(),
            Invite.accepted_at.is_(None),
            Invite.revoked_at.is_(None),
        ).order_by(Invite.created_at.desc())
    )
    inv = result.scalars().first()
    return FEPendingInvite(
        id=str(inv.id), email=inv.email, role=inv.role.value,
        invitedLabel=helpers.time_ago(inv.created_at),
    )

@router.delete("/members/invites/{invite_id}", response_model=FESuccessResponse)
async def revoke_member_invite(
    invite_id: str,
    current_user: User = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> FESuccessResponse:
    try:
        iid = uuid.UUID(invite_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found.")
    await team_service.revoke_invite(iid, current_user.tenant_id, db)
    return FESuccessResponse()

@router.delete("/members/{user_id}", response_model=FESuccessResponse)
async def remove_member(
    user_id: str,
    current_user: User = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> FESuccessResponse:
    """Remove an agent from the workspace. Owners can't remove themselves here."""
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found.")
    if uid == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You can't remove yourself.",
        )
    membership = await db.execute(
        select(TenantUser).where(
            TenantUser.tenant_id == current_user.tenant_id, TenantUser.user_id == uid
        )
    )
    tu = membership.scalar_one_or_none()
    if tu is None or tu.role == TenantRole.owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found.")

    member = await db.execute(select(User).where(User.id == uid))
    member_user = member.scalar_one_or_none()
    if member_user is not None:
        # Kill their sessions immediately and free any chats they were handling.
        member_user.token_version = (member_user.token_version or 0) + 1
        member_user.is_active = False
        invalidate_user_cache(member_user.id)
    await db.execute(
        _sa_update(Conversation)
        .where(Conversation.assigned_user_id == uid)
        .values(assigned_user_id=None, is_live=False)
    )
    await db.delete(tu)
    await db.flush()
    return FESuccessResponse()
