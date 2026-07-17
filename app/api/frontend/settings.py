"""
Frontend-compat settings endpoints (/api/settings/...).

  Workspace:      GET/PUT/DELETE /api/settings/workspace
  Notifications:  GET/PUT        /api/settings/notifications   (PUT takes a partial patch)
  Billing:        GET            /api/settings/billing
                  POST           /api/settings/billing/cancel
                  PUT            /api/settings/billing/plan
                  PUT            /api/settings/billing/payment-method
  API keys:       GET/POST       /api/settings/api-keys
                  DELETE         /api/settings/api-keys/{id}
"""

import asyncio
import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.frontend import helpers
from app.core.security import hash_password
from app.database import get_db
from app.models.subscription import BillingCycle, PlanName, SubscriptionStatus
from app.models.tenant import Tenant
from app.models.tenant_settings import ApiKey, NotificationSetting, PaymentMethod
from app.models.user import User
from app.schemas.frontend import (
    FEApiKey,
    FEBillingInfo,
    FEChangePlanRequest,
    FECreateApiKeyRequest,
    FECreateApiKeyResponse,
    FENotificationSettings,
    FENotificationSettingsPatch,
    FEPaymentMethod,
    FESaveWorkspaceSettingsResponse,
    FESuccessResponse,
    FEUpdatePaymentMethodRequest,
    FEWorkspaceSettings,
)
from app.services.auth_service import PLAN_QUOTAS, _slugify, get_current_user

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
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FESuccessResponse:
    """Soft-delete: deactivates the tenant and the user so every later request is rejected."""
    tenant = await helpers.get_tenant(current_user, db)
    tenant.is_active = False
    current_user.is_active = False
    await db.flush()
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

    fe_status = "Active"
    if subscription is not None and subscription.status == SubscriptionStatus.cancelled:
        fe_status = "Canceled"

    renews_on = helpers.first_of_next_month()
    if subscription is not None and subscription.expires_at is not None:
        renews_on = subscription.expires_at

    return FEBillingInfo(
        planId=fe_plan,
        status=fe_status,
        priceLabel=helpers.PLAN_PRICE_LABELS[fe_plan],
        renewsOn=helpers.format_date(renews_on),
        paymentMethod=FEPaymentMethod(brand=payment.brand, last4=payment.last4, expiry=payment.expiry),
    )

@router.get("/billing", response_model=FEBillingInfo)
async def get_billing(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEBillingInfo:
    return await _billing_info(current_user, db)

@router.post("/billing/cancel", response_model=FEBillingInfo)
async def cancel_plan(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEBillingInfo:
    subscription = await helpers.get_subscription(current_user.tenant_id, db)
    if subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No subscription found.")
    subscription.status = SubscriptionStatus.cancelled
    await db.flush()
    return await _billing_info(current_user, db)

@router.put("/billing/plan", response_model=FEBillingInfo)
async def change_plan(
    body: FEChangePlanRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEBillingInfo:
    subscription = await helpers.get_subscription(current_user.tenant_id, db)
    if subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No subscription found.")

    plan_name = helpers.FE_TO_BE_PLAN[body.planId]
    subscription.plan_name = plan_name
    subscription.billing_cycle = None if plan_name == PlanName.free else BillingCycle.monthly
    subscription.status = SubscriptionStatus.active

    quota = await helpers.get_quota(current_user.tenant_id, db)
    quotas = PLAN_QUOTAS[plan_name]
    if quota is not None:
        quota.max_documents = quotas["max_documents"]
        quota.max_file_size_mb = quotas["max_file_size_mb"]
        quota.max_questions_per_month = quotas["max_questions_per_month"]

    await db.flush()
    return await _billing_info(current_user, db)

@router.put("/billing/payment-method", response_model=FEBillingInfo)
async def update_payment_method(
    body: FEUpdatePaymentMethodRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEBillingInfo:
    payment = await _get_or_create_payment_method(current_user, db)
    payment.last4 = body.last4
    payment.expiry = body.expiry
    await db.flush()
    return await _billing_info(current_user, db)

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
async def create_api_key(
    body: FECreateApiKeyRequest,
    current_user: User = Depends(get_current_user),
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
    current_user: User = Depends(get_current_user),
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
