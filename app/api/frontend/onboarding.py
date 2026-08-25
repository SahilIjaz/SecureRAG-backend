"""
Frontend-compat onboarding endpoints (/api/onboarding/...).

  - POST /api/onboarding/step/{n}  -> {success}      (n = 1..5, saves partial progress)
  - POST /api/onboarding/upload    -> {success, uploadedFiles, uploadedUrls}
  - POST /api/onboarding/complete  -> {success, summary}
  - GET  /api/onboarding/summary   -> summary | null (Profile page read-back)
  - PUT  /api/onboarding/summary   -> {success, summary}

All routes accept the access token issued at OTP verification (the frontend
keeps a single token for the whole session), so they use get_any_valid_user.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.frontend import helpers
from app.core import perf_timing
from app.core.rate_limit import limiter
from app.database import get_db
from app.models.document import Document, DocumentSource, DocumentStatus
from app.models.subscription import PlanName, Subscription, SubscriptionStatus
from app.models.tenant import Tenant
from app.models.tenant_quota import TenantQuota
from app.models.usage_count import UsageCount
from app.models.user import User
from app.schemas.frontend import (
    FECheckoutRequest,
    FECheckoutResponse,
    FECompleteOnboardingRequest,
    FECompleteOnboardingResponse,
    FEOnboardingSummary,
    FESaveOnboardingDetailsRequest,
    FESuccessResponse,
    FEUploadDocumentsResponse,
)
from app.services import document_service, stripe_service
from app.services.auth_service import PLAN_QUOTAS, get_any_valid_user, _first_of_month, _slugify
from app.services.indexing_service import schedule_document_processing, schedule_url_scrape

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/onboarding", tags=["Frontend — Onboarding"])

async def _unique_slug(name: str, tenant: Tenant, db: AsyncSession) -> str:
    base_slug = _slugify(name) or "workspace"
    slug = base_slug
    counter = 1
    while True:
        result = await db.execute(
            select(Tenant).where(Tenant.slug == slug, Tenant.id != tenant.id)
        )
        if result.scalar_one_or_none() is None:
            return slug
        slug = f"{base_slug}-{counter}"
        counter += 1

async def _ensure_baseline_quota(tenant: Tenant, db: AsyncSession) -> TenantQuota:
    """
    Guarantees a TenantQuota (and UsageCount) row exists before any Step-5
    upload/scrape can happen. Without this, document_service.upload_documents'
    `if quota: ...` check silently no-ops when no quota row exists yet,
    letting a brand-new signup upload unlimited documents before any plan
    limit exists.

    There's no permanent free tier anymore — POST /onboarding/checkout
    (Step 3) is what creates the Subscription row, via a real Stripe trial.
    If none exists by Step 5, the wizard was driven out of order; fail
    loudly here rather than silently granting a free plan that was never
    supposed to exist. The TenantQuota row itself may still be a
    conservative Starter-tier placeholder at this point if Stripe's webhook
    hasn't confirmed the actual chosen plan yet — that's expected and
    self-corrects once the webhook lands (see stripe_service.sync_subscription_from_stripe).
    """
    subscription = await helpers.get_subscription(tenant.id, db)
    if subscription is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Complete plan selection (Step 3) before uploading documents.",
        )

    quota, usage = await helpers.get_quota_and_usage(tenant.id, db)
    if quota is None:
        starter_quotas = PLAN_QUOTAS[PlanName.free]
        quota = TenantQuota(tenant_id=tenant.id, subscription_id=subscription.id, **starter_quotas)
        db.add(quota)
        await db.flush()

    if usage is None:
        usage = UsageCount(tenant_id=tenant.id, period_month=_first_of_month().date())
        db.add(usage)
        await db.flush()

    return quota

async def _build_summary(user: User, db: AsyncSession) -> Optional[FEOnboardingSummary]:
    tenant, subscription = await helpers.get_tenant_and_subscription(user, db)
    if subscription is None:
        return None

    docs = await helpers.get_active_documents(user.tenant_id, db)
    file_docs, url_docs, _faq_docs = helpers.split_docs_and_urls(docs)
    completed_at = tenant.onboarding_completed_at or subscription.started_at

    return FEOnboardingSummary(
        businessCategory=tenant.business_category or "",
        teamSize=tenant.employee_count_range or "",
        workspaceName=tenant.workspace_name if tenant.workspace_name != "__pending__" else "",
        plan=helpers.fe_plan_for_subscription(subscription),
        hasDocuments=tenant.has_documents if tenant.has_documents is not None else bool(docs),
        uploadedFiles=len(file_docs),
        uploadedUrls=len(url_docs),
        completedAt=completed_at.isoformat() if completed_at else "",
    )

@router.post("/step/{step}", response_model=FESuccessResponse)
async def save_step(
    step: int,
    payload: dict = Body(default={}),
    current_user: User = Depends(get_any_valid_user),
    db: AsyncSession = Depends(get_db),
) -> FESuccessResponse:
    if step not in (1, 2, 3, 4, 5):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid onboarding step.")

    with perf_timing.timed("db_get_tenant"):
        tenant = await helpers.get_tenant(current_user, db)

    if step == 1:
        category = str(payload.get("businessCategory", "")).strip()
        team_size = str(payload.get("teamSize", "")).strip()
        if not category or not team_size:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="businessCategory and teamSize are required.")
        tenant.business_category = category
        tenant.employee_count_range = team_size
    elif step == 2:
        workspace_name = str(payload.get("workspaceName", "")).strip()
        if len(workspace_name) < 2:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="workspaceName must be at least 2 characters.")
        tenant.workspace_name = workspace_name
        tenant.slug = await _unique_slug(workspace_name, tenant, db)
    elif step == 3:
        if payload.get("plan") not in helpers.FE_TO_BE_PLAN:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="plan must be one of: starter, growth, business.")
        # The Stripe trial subscription itself is started via POST
        # /onboarding/checkout (called once the user has entered card
        # details), not here — this step only validates the choice.
    elif step == 4:
        has_documents = payload.get("hasDocuments")
        if not isinstance(has_documents, bool):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="hasDocuments must be a boolean.")
        tenant.has_documents = has_documents
    # step 5 (upload) is handled by POST /onboarding/upload.

    with perf_timing.timed("db_flush"):
        await db.flush()
    return FESuccessResponse()

@router.post("/checkout", response_model=FECheckoutResponse)
async def onboarding_checkout(
    body: FECheckoutRequest,
    current_user: User = Depends(get_any_valid_user),
    db: AsyncSession = Depends(get_db),
) -> FECheckoutResponse:
    """
    Starts the 3-day trial for the plan picked in Step 3 and returns a
    Stripe SetupIntent client secret for the frontend to collect the card
    via Stripe Elements, mirroring POST /api/settings/billing/checkout —
    kept as a separate endpoint only because this router authenticates via
    get_any_valid_user (mid-wizard token) rather than get_current_user.
    """
    with perf_timing.timed("db_get_tenant"):
        tenant = await helpers.get_tenant(current_user, db)
    with perf_timing.timed("db_get_subscription"):
        subscription = await helpers.get_subscription(tenant.id, db)
    if subscription is None:
        subscription = Subscription(
            tenant_id=tenant.id,
            plan_name=PlanName.free,
            billing_cycle=None,
            status=SubscriptionStatus.trial,
            expires_at=None,
        )
        db.add(subscription)
        with perf_timing.timed("db_flush_new_subscription"):
            await db.flush()

    plan_name = helpers.FE_TO_BE_PLAN[body.planId]
    with perf_timing.timed("stripe_start_trial_subscription_total"):
        client_secret = await stripe_service.start_trial_subscription(
            tenant, current_user, subscription, plan_name, db
        )
    return FECheckoutResponse(clientSecret=client_secret)

@router.post("/upload", response_model=FEUploadDocumentsResponse)
@limiter.limit("10/minute")
async def upload(
    request: Request,
    files: List[UploadFile] = File(default=[]),
    urls: List[str] = Form(default=[]),
    current_user: User = Depends(get_any_valid_user),
    db: AsyncSession = Depends(get_db),
) -> FEUploadDocumentsResponse:
    with perf_timing.timed("db_get_tenant"):
        tenant = await helpers.get_tenant(current_user, db)
    with perf_timing.timed("ensure_baseline_quota"):
        quota = await _ensure_baseline_quota(tenant, db)
    if quota is None:
        # Fail closed rather than silently allowing unlimited uploads —
        # _ensure_baseline_quota should always create one, so this would
        # indicate a bug in that function, not a normal runtime state.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not initialize workspace quota.",
        )

    uploaded_files = 0
    uploaded_urls = 0
    to_index: List[Document] = []

    if files:
        with perf_timing.timed("upload_documents_total"):
            saved = await document_service.upload_documents(current_user, files, db)
        uploaded_files = len(saved)
        to_index.extend(saved)

    url_docs: List[Document] = []
    if urls:
        with perf_timing.timed("create_pending_url_documents"):
            url_docs = await _create_pending_url_documents(current_user, urls, db, quota=quota)
        uploaded_urls = len(url_docs)
        to_index.extend(url_docs)

    with perf_timing.timed("db_commit"):
        await db.commit()
    with perf_timing.timed("schedule_background_jobs"):
        schedule_document_processing([d.id for d in to_index if d.file_url])
        schedule_url_scrape([d.id for d in url_docs])

    return FEUploadDocumentsResponse(
        success=True,
        uploadedFiles=uploaded_files,
        uploadedUrls=uploaded_urls,
    )

async def _create_pending_url_documents(
    user: User,
    urls: List[str],
    db: AsyncSession,
    quota: Optional[TenantQuota] = None,
) -> List[Document]:
    """
    Creates one pending Document per URL and returns immediately — the actual
    scrape (headless browser, up to CRAWL4AI_TIMEOUT seconds each) runs in the
    background via schedule_url_scrape. Previously this scraped every URL
    synchronously and sequentially before responding, so onboarding with N
    URLs could block for up to N x ~60s; a failed scrape is caught there and
    marks that document Failed, same tolerant behavior as before.

    `quota` (if provided — callers outside onboarding may not have one yet)
    is enforced as a hard document-count cap before any rows are created.
    """
    if quota is None:
        quota = await document_service.lock_quota_row(user.tenant_id, db)

    if quota and quota.max_documents != -1:
        result = await db.execute(
            select(func.count()).select_from(Document).where(
                Document.tenant_id == user.tenant_id,
                Document.is_active == True,
            )
        )
        existing_count = result.scalar_one()
        if existing_count + len(urls) > quota.max_documents:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Document quota exceeded. Your plan allows {quota.max_documents} documents.",
            )

    saved: List[Document] = []
    for url in urls:
        url = url.strip()
        if not url:
            continue
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"

        doc = Document(
            tenant_id=user.tenant_id,
            original_filename=url,
            file_size_mb=0.0,
            mime_type="application/pdf",
            source=DocumentSource.scraped,
            source_url=url,
            status=DocumentStatus.pending,
        )
        db.add(doc)
        saved.append(doc)

    await db.flush()
    return saved

@router.post("/complete", response_model=FECompleteOnboardingResponse, status_code=status.HTTP_201_CREATED)
async def complete(
    body: FECompleteOnboardingRequest,
    current_user: User = Depends(get_any_valid_user),
    db: AsyncSession = Depends(get_db),
) -> FECompleteOnboardingResponse:
    with perf_timing.timed("db_get_tenant"):
        tenant = await helpers.get_tenant(current_user, db)

    tenant.business_category = body.businessCategory.strip()
    tenant.employee_count_range = body.teamSize.strip()
    tenant.workspace_name = body.workspaceName.strip()
    with perf_timing.timed("unique_slug_lookup"):
        tenant.slug = await _unique_slug(body.workspaceName, tenant, db)
    tenant.has_documents = body.hasDocuments
    tenant.onboarding_completed_at = datetime.now(timezone.utc)

    # The trial Subscription itself is created by POST /onboarding/checkout
    # when Step 3 was submitted (real Stripe trial, no permanent free plan
    # anymore) — /complete no longer creates one. plan_name/status/
    # billing_cycle are left for Stripe's webhook to set (see
    # stripe_service.sync_subscription_from_stripe); this endpoint only
    # proactively syncs the quota to the already-chosen plan so the
    # dashboard doesn't sit on the conservative placeholder until the
    # webhook round trip lands.
    with perf_timing.timed("db_get_subscription"):
        subscription = await helpers.get_subscription(tenant.id, db)
    if subscription is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Complete plan selection (Step 3) before finishing onboarding.",
        )

    # SECURITY: derive the plan from the Stripe price on the subscription row
    # (set server-side at /checkout), never from body.plan. Trusting body.plan
    # let a client POST plan="business" and receive Business quotas for free.
    plan_name = stripe_service.plan_from_price_id(subscription.stripe_price_id)
    if plan_name is None:
        # No confirmed Stripe price yet (e.g. checkout not completed) — fall
        # back to whatever plan the subscription row already carries, which is
        # the conservative placeholder until the webhook confirms the trial.
        plan_name = subscription.plan_name
    quotas = PLAN_QUOTAS[plan_name]

    with perf_timing.timed("db_get_quota_and_usage"):
        quota, usage = await helpers.get_quota_and_usage(tenant.id, db)
    if quota is None:
        quota = TenantQuota(tenant_id=tenant.id, subscription_id=subscription.id, **quotas)
        db.add(quota)
    else:
        quota.max_documents = quotas["max_documents"]
        quota.max_file_size_mb = quotas["max_file_size_mb"]
        quota.max_questions_per_month = quotas["max_questions_per_month"]

    if usage is None:
        usage = UsageCount(tenant_id=tenant.id, period_month=_first_of_month().date())
        db.add(usage)
        with perf_timing.timed("db_flush_new_usage"):
            await db.flush()

    # One-time reconciliation, not an ongoing mechanism: Step-5 uploads can
    # land before this point (via _ensure_baseline_quota), so recompute
    # documents_count/storage_used_mb from the tenant's actual Document rows
    # rather than trusting whatever upload_documents/_scrape_urls_tolerant
    # already incremented — self-healing regardless of exactly when the
    # UsageCount row was created relative to those uploads. This does not
    # replace the row-locking fix in document_service.lock_quota_row, which
    # is what prevents counters from drifting going forward; if usage is
    # still found drifting after both are in place, that's a new bug, not
    # something this reconciliation should be relied on to keep masking.
    with perf_timing.timed("db_get_active_documents"):
        result = await db.execute(
            select(Document).where(
                Document.tenant_id == tenant.id,
                Document.is_active == True,
            )
        )
        active_docs = result.scalars().all()
    usage.documents_count = len(active_docs)
    usage.storage_used_mb = round(sum(d.file_size_mb or 0.0 for d in active_docs), 4)

    with perf_timing.timed("db_flush_final"):
        await db.flush()

    with perf_timing.timed("build_summary"):
        summary = await _build_summary(current_user, db)
    return FECompleteOnboardingResponse(success=True, summary=summary)

@router.get("/summary", response_model=Optional[FEOnboardingSummary])
async def get_summary(
    current_user: User = Depends(get_any_valid_user),
    db: AsyncSession = Depends(get_db),
) -> Optional[FEOnboardingSummary]:
    return await _build_summary(current_user, db)

@router.put("/summary", response_model=FECompleteOnboardingResponse)
async def save_details(
    body: FESaveOnboardingDetailsRequest,
    current_user: User = Depends(get_any_valid_user),
    db: AsyncSession = Depends(get_db),
) -> FECompleteOnboardingResponse:
    tenant = await helpers.get_tenant(current_user, db)
    tenant.business_category = body.businessCategory.strip()
    tenant.employee_count_range = body.teamSize.strip()
    await db.flush()

    summary = await _build_summary(current_user, db)
    if summary is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Onboarding has not been completed yet.",
        )
    return FECompleteOnboardingResponse(success=True, summary=summary)
