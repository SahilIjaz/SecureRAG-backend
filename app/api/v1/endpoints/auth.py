"""
Auth endpoints — complete signup flow + signin + refresh + me.

POST /api/v1/auth/signup          — Step 1: register user, send OTP
POST /api/v1/auth/verify-email    — Step 2: verify OTP
POST /api/v1/auth/resend-otp      — Step 2: resend OTP
POST /api/v1/auth/organization    — Step 3: save business category + employee count
POST /api/v1/auth/workspace       — Step 4: set workspace name
POST /api/v1/auth/select-plan     — Step 5: choose subscription plan (issues tokens)
POST /api/v1/auth/signin          — Login with email + password
POST /api/v1/auth/refresh         — Exchange refresh token for new token pair
GET  /api/v1/auth/me              — Get current authenticated user profile
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.schemas.auth import (
    MessageResponse,
    OnboardingCompleteResponse,
    OTPVerifyRequest,
    OrganizationInfoRequest,
    PlanSelectionRequest,
    RefreshTokenRequest,
    ResendOTPRequest,
    SigninRequest,
    SignupStep1Request,
    TokenResponse,
    WorkspaceSetupRequest,
)
from app.schemas.user import UserWithTenantResponse
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["Authentication"])


# ---------------------------------------------------------------------------
# Step 1 — Register
# ---------------------------------------------------------------------------

@router.post(
    "/signup",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Step 1 — Register a new account",
    description=(
        "Creates an unverified user account and sends a 4-digit OTP "
        "to the provided email address. The user must verify the OTP "
        "in Step 2 before proceeding."
    ),
)
async def signup(
    body: SignupStep1Request,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    result = await auth_service.register_user(
        full_name=body.full_name,
        email=body.email,
        password=body.password,
        db=db,
    )
    return MessageResponse(**result)


# ---------------------------------------------------------------------------
# Step 2 — Verify email OTP
# ---------------------------------------------------------------------------

@router.post(
    "/verify-email",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Step 2 — Verify email with OTP",
    description=(
        "Validates the 4-digit OTP sent to the user's email. "
        "The OTP expires after 10 minutes. On success the email is "
        "marked verified and the user may proceed to Step 3."
    ),
)
async def verify_email(
    body: OTPVerifyRequest,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    result = await auth_service.verify_email(
        email=body.email,
        otp_code=body.otp_code,
        db=db,
    )
    return MessageResponse(**result)


@router.post(
    "/resend-otp",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Step 2 — Resend OTP",
    description=(
        "Invalidates all previous OTPs for the email and sends a fresh one. "
        "Rate-limiting should be applied in production."
    ),
)
async def resend_otp(
    body: ResendOTPRequest,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    result = await auth_service.resend_otp(email=body.email, db=db)
    return MessageResponse(**result)


# ---------------------------------------------------------------------------
# Step 3 — Organisation info
# ---------------------------------------------------------------------------

@router.post(
    "/organization",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Step 3 — Save organisation info",
    description=(
        "Saves the business category and employee count range "
        "to the tenant record. Requires a verified email."
    ),
)
async def organization_info(
    body: OrganizationInfoRequest,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    result = await auth_service.save_organization_info(
        email=body.email,
        business_category=body.business_category,
        employee_count_range=body.employee_count_range,
        db=db,
    )
    return MessageResponse(**result)


# ---------------------------------------------------------------------------
# Step 4 — Workspace setup
# ---------------------------------------------------------------------------

@router.post(
    "/workspace",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Step 4 — Set up workspace",
    description=(
        "Sets the workspace name and auto-generates a unique URL-safe slug "
        "for the tenant. Requires a verified email."
    ),
)
async def setup_workspace(
    body: WorkspaceSetupRequest,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    result = await auth_service.setup_workspace(
        email=body.email,
        workspace_name=body.workspace_name,
        db=db,
    )
    return MessageResponse(message=result["message"], email=body.email)


# ---------------------------------------------------------------------------
# Step 5 — Select plan (completes onboarding, issues tokens)
# ---------------------------------------------------------------------------

@router.post(
    "/select-plan",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Step 5 — Select subscription plan (completes onboarding)",
    description=(
        "Creates the subscription and quota records, seeds the monthly usage "
        "counter, and returns a JWT access + refresh token pair. "
        "This is the final onboarding step — after this the user is fully logged in."
    ),
)
async def select_plan(
    body: PlanSelectionRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    result = await auth_service.select_plan(
        email=body.email,
        plan_name=body.plan_name,
        billing_cycle=body.billing_cycle,
        db=db,
    )
    return TokenResponse(
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
    )


# ---------------------------------------------------------------------------
# Sign in
# ---------------------------------------------------------------------------

@router.post(
    "/signin",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Sign in with email and password",
    description=(
        "Authenticates an existing user and returns a JWT access token "
        "(short-lived) and a refresh token (long-lived). "
        "Attach the access token as: Authorization: Bearer <token>"
    ),
)
async def signin(
    body: SigninRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    result = await auth_service.signin(
        email=body.email,
        password=body.password,
        db=db,
    )
    return TokenResponse(**result)


# ---------------------------------------------------------------------------
# Refresh tokens
# ---------------------------------------------------------------------------

@router.post(
    "/refresh",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Refresh access token",
    description=(
        "Exchange a valid refresh token for a new access + refresh token pair. "
        "The old refresh token is consumed."
    ),
)
async def refresh_tokens(
    body: RefreshTokenRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    result = await auth_service.refresh_tokens(
        refresh_token=body.refresh_token,
        db=db,
    )
    return TokenResponse(**result)


# ---------------------------------------------------------------------------
# Get current user — protected route example
# ---------------------------------------------------------------------------

@router.get(
    "/me",
    response_model=UserWithTenantResponse,
    status_code=status.HTTP_200_OK,
    summary="Get current user profile",
    description=(
        "Returns the authenticated user's profile and tenant info. "
        "Requires a valid Bearer access token."
    ),
)
async def get_me(
    current_user: User = Depends(auth_service.get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserWithTenantResponse:
    from sqlalchemy.orm import selectinload
    from sqlalchemy import select
    from app.models.user import User as UserModel

    # Reload user with tenant relationship eagerly loaded
    result = await db.execute(
        select(UserModel)
        .options(selectinload(UserModel.tenant))
        .where(UserModel.id == current_user.id)
    )
    user = result.scalar_one()

    return UserWithTenantResponse(
        id=user.id,
        full_name=user.full_name,
        email=user.email,
        is_email_verified=user.is_email_verified,
        is_active=user.is_active,
        created_at=user.created_at,
        tenant_id=user.tenant_id,
        workspace_name=user.tenant.workspace_name if user.tenant else None,
        slug=user.tenant.slug if user.tenant else None,
    )
