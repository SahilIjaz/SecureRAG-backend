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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.api.frontend import helpers
from app.database import get_db
from app.models.document import Document, DocumentSource, DocumentStatus
from app.models.subscription import BillingCycle, PlanName, Subscription, SubscriptionStatus
from app.models.tenant import Tenant
from app.models.tenant_quota import TenantQuota
from app.models.usage_count import UsageCount
from app.models.user import User
from app.schemas.frontend import (
    FECompleteOnboardingRequest,
    FECompleteOnboardingResponse,
    FEOnboardingSummary,
    FESaveOnboardingDetailsRequest,
    FESuccessResponse,
    FEUploadDocumentsResponse,
)
from app.services import document_service
from app.services.auth_service import PLAN_QUOTAS, get_any_valid_user, _first_of_month, _slugify
from app.services.indexing_service import schedule_document_processing

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/onboarding", tags=["Frontend — Onboarding"])
limiter = Limiter(key_func=get_remote_address)

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

async def _build_summary(user: User, db: AsyncSession) -> Optional[FEOnboardingSummary]:
    tenant = await helpers.get_tenant(user, db)
    subscription = await helpers.get_subscription(user.tenant_id, db)
    if subscription is None:
        return None

    docs = await helpers.get_active_documents(user.tenant_id, db)
    file_docs, url_docs = helpers.split_docs_and_urls(docs)
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
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="plan must be one of: free, pro, premium.")
        # Plan is persisted on /complete (which receives it again) — nothing to save yet.
    elif step == 4:
        has_documents = payload.get("hasDocuments")
        if not isinstance(has_documents, bool):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="hasDocuments must be a boolean.")
        tenant.has_documents = has_documents
    # step 5 (upload) is handled by POST /onboarding/upload.

    await db.flush()
    return FESuccessResponse()

@router.post("/upload", response_model=FEUploadDocumentsResponse)
@limiter.limit("10/minute")
async def upload(
    request: Request,
    files: List[UploadFile] = File(default=[]),
    urls: List[str] = Form(default=[]),
    current_user: User = Depends(get_any_valid_user),
    db: AsyncSession = Depends(get_db),
) -> FEUploadDocumentsResponse:
    uploaded_files = 0
    uploaded_urls = 0
    to_index: List[Document] = []

    if files:
        saved = await document_service.upload_documents(current_user, files, db)
        uploaded_files = len(saved)
        to_index.extend(saved)

    if urls:
        saved_urls = await _scrape_urls_tolerant(current_user, urls, db)
        uploaded_urls = len(saved_urls)
        to_index.extend(saved_urls)

    await db.commit()
    schedule_document_processing(
        [d.id for d in to_index if d.status != DocumentStatus.failed and d.file_url]
    )

    return FEUploadDocumentsResponse(
        success=True,
        uploadedFiles=uploaded_files,
        uploadedUrls=uploaded_urls,
    )

async def _scrape_urls_tolerant(user: User, urls: List[str], db: AsyncSession) -> List[Document]:
    """
    Scrape each URL into a document like document_service.scrape_and_add_documents,
    but never fail the whole request: a URL that can't be scraped is recorded as a
    Failed document so the dashboard still shows it.
    """
    from app.config import settings
    from app.core.scraper import scrape_website_to_pdf
    from app.core.storage import upload_file_to_cloudinary

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
        try:
            pdf_content, page_title = await scrape_website_to_pdf(url, timeout=settings.CRAWL4AI_TIMEOUT)
            public_id, secure_url = await upload_file_to_cloudinary(
                file_content=pdf_content,
                tenant_id=user.tenant_id,
                original_filename=f"{page_title}.pdf",
                content_type="application/pdf",
            )
            doc.original_filename = page_title
            doc.file_path = public_id
            doc.file_url = secure_url
            doc.file_size_mb = round(len(pdf_content) / (1024 * 1024), 4)
        except Exception as e:
            logger.warning("Scrape failed for %s: %s", url, e)
            doc.status = DocumentStatus.failed

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
    tenant = await helpers.get_tenant(current_user, db)

    tenant.business_category = body.businessCategory.strip()
    tenant.employee_count_range = body.teamSize.strip()
    tenant.workspace_name = body.workspaceName.strip()
    tenant.slug = await _unique_slug(body.workspaceName, tenant, db)
    tenant.has_documents = body.hasDocuments
    tenant.onboarding_completed_at = datetime.now(timezone.utc)

    plan_name = helpers.FE_TO_BE_PLAN[body.plan]
    quotas = PLAN_QUOTAS[plan_name]
    # The dashboard's plans are monthly-priced; the DB requires a billing
    # cycle for any paid plan (chk_subscriptions_billing_cycle).
    billing_cycle = None if plan_name == PlanName.free else BillingCycle.monthly

    subscription = await helpers.get_subscription(tenant.id, db)
    if subscription is None:
        subscription = Subscription(
            tenant_id=tenant.id,
            plan_name=plan_name,
            billing_cycle=billing_cycle,
            status=SubscriptionStatus.active,
            expires_at=None,
        )
        db.add(subscription)
        await db.flush()
    else:
        # Re-completing onboarding just updates the plan — never 409s the flow.
        subscription.plan_name = plan_name
        subscription.billing_cycle = billing_cycle
        subscription.status = SubscriptionStatus.active

    quota = await helpers.get_quota(tenant.id, db)
    if quota is None:
        quota = TenantQuota(tenant_id=tenant.id, subscription_id=subscription.id, **quotas)
        db.add(quota)
    else:
        quota.max_documents = quotas["max_documents"]
        quota.max_file_size_mb = quotas["max_file_size_mb"]
        quota.max_questions_per_month = quotas["max_questions_per_month"]

    usage = await helpers.get_current_usage(tenant.id, db)
    if usage is None:
        db.add(UsageCount(tenant_id=tenant.id, period_month=_first_of_month().date()))

    await db.flush()

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
