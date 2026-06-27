"""
Auth endpoints — complete signup flow + signin + refresh + me.

POST /api/v1/auth/signup          — Step 1: register user, send OTP
POST /api/v1/auth/verify-email    — Step 2: verify OTP
POST /api/v1/auth/resend-otp      — Step 2: resend OTP
POST /api/v1/auth/organization    — Step 3: save business category + employee count
POST /api/v1/auth/workspace       — Step 4: set workspace name
POST /api/v1/auth/select-plan     — Step 5: choose subscription plan (issues tokens)
POST /api/v1/auth/signin          — Login with email + password
POST /api/v1/auth/social/google   — Sign in or register with Google
POST /api/v1/auth/refresh         — Exchange refresh token for new token pair
GET  /api/v1/auth/me              — Get current authenticated user profile
"""

from fastapi import APIRouter, Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.database import get_db
from app.models.user import User
from app.schemas.auth import (
    ForgotPasswordRequest,
    GoogleLoginRequest,
    MessageResponse,
    OnboardingCompleteResponse,
    OTPVerifyRequest,
    OrganizationInfoRequest,
    OrganizationInfoResponse,
    OTPVerifyResponse,
    PlanSelectionRequest,
    RefreshTokenRequest,
    ResendOTPRequest,
    ResetPasswordRequest,
    SigninRequest,
    SignupStep1Request,
    SocialLoginResponse,
    TokenResponse,
    VerifyResetOTPRequest,
    VerifyResetOTPResponse,
    WorkspaceSetupRequest,
)
from app.schemas.user import UserWithTenantResponse
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["Authentication"])
limiter = Limiter(key_func=get_remote_address)



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
@limiter.limit("3/minute")
async def signup(
    request: Request,
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



@router.post(
    "/verify-email",
    response_model=OTPVerifyResponse,
    status_code=status.HTTP_200_OK,
    summary="Step 2 — Verify email with OTP",
    description=(
        "Validates the 4-digit OTP. On success returns an onboarding_token "
        "(valid 1 hour). Pass this as Bearer token in steps 3, 4, and 5."
    ),
)
@limiter.limit("5/minute")
async def verify_email(
    request: Request,
    body: OTPVerifyRequest,
    db: AsyncSession = Depends(get_db),
) -> OTPVerifyResponse:
    result = await auth_service.verify_email(
        email=body.email,
        otp_code=body.otp_code,
        db=db,
    )
    return OTPVerifyResponse(**result)


@router.post(
    "/resend-otp",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Step 2 — Resend OTP",
    description=(
        "Invalidates all previous OTPs for the email and sends a fresh one."
    ),
)
async def resend_otp(
    body: ResendOTPRequest,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    result = await auth_service.resend_otp(email=body.email, db=db)
    return MessageResponse(**result)



@router.post(
    "/organization",
    response_model=OrganizationInfoResponse,
    status_code=status.HTTP_200_OK,
    summary="Step 3 — Save organisation info",
    description=(
        "Saves business category and employee count. "
        "Requires Authorization: Bearer <onboarding_token> from Step 2."
    ),
)
async def organization_info(
    body: OrganizationInfoRequest,
    current_user: User = Depends(auth_service.get_onboarding_user),
    db: AsyncSession = Depends(get_db),
) -> OrganizationInfoResponse:
    result = await auth_service.save_organization_info(
        user=current_user,
        business_category=body.business_category,
        employee_count_range=body.employee_count_range,
        db=db,
    )
    return OrganizationInfoResponse(**result)



@router.post(
    "/workspace",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Step 4 — Set up workspace",
    description=(
        "Sets the workspace name and generates a unique slug. "
        "Requires Authorization: Bearer <onboarding_token> from Step 2."
    ),
)
async def setup_workspace(
    body: WorkspaceSetupRequest,
    current_user: User = Depends(auth_service.get_onboarding_user),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    result = await auth_service.setup_workspace(
        user=current_user,
        workspace_name=body.workspace_name,
        db=db,
    )
    return MessageResponse(message=result["message"], email=current_user.email)



@router.post(
    "/select-plan",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Step 5 — Select subscription plan (completes onboarding)",
    description=(
        "Creates subscription + quota records, seeds usage counter. "
        "Returns a full access + refresh token pair — onboarding is complete. "
        "Requires Authorization: Bearer <onboarding_token> from Step 2."
    ),
)
async def select_plan(
    body: PlanSelectionRequest,
    current_user: User = Depends(auth_service.get_onboarding_user),
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    result = await auth_service.select_plan(
        user=current_user,
        plan_name=body.plan_name,
        billing_cycle=body.billing_cycle,
        db=db,
    )
    return TokenResponse(
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
    )



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
@limiter.limit("5/minute")
async def signin(
    request: Request,
    body: SigninRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    result = await auth_service.signin(
        email=body.email,
        password=body.password,
        db=db,
    )
    return TokenResponse(**result)



@router.post(
    "/social/google",
    response_model=SocialLoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Sign in or register with Google",
    description=(
        "Accepts a Google ID token (obtained from Google Sign-In on the frontend). "
        "If the user exists and onboarding is complete, returns JWT tokens. "
        "If the user is new or onboarding is incomplete, returns an onboarding_token "
        "so the frontend can continue the onboarding flow (steps 3-5)."
    ),
)
async def google_login(
    body: GoogleLoginRequest,
    db: AsyncSession = Depends(get_db),
) -> SocialLoginResponse:
    result = await auth_service.google_login(token=body.id_token, db=db)
    return SocialLoginResponse(**result)



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
    result = await db.execute(
        select(User)
        .options(selectinload(User.tenant))
        .where(User.id == current_user.id)
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



@router.post(
    "/forgot-password",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Step 1 — Request password reset OTP",
    description=(
        "Sends a 4-digit OTP to the provided email address. "
        "Always returns a success message regardless of whether the account exists "
        "to prevent user enumeration."
    ),
)
async def forgot_password(
    body: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    result = await auth_service.forgot_password(email=body.email, db=db)
    return MessageResponse(**result)



@router.post(
    "/verify-reset-otp",
    response_model=VerifyResetOTPResponse,
    status_code=status.HTTP_200_OK,
    summary="Step 2 — Verify reset OTP",
    description=(
        "Validates the 4-digit OTP sent to the email. "
        "On success returns a short-lived reset_token (valid for 15 minutes) "
        "that must be sent with the new password in Step 3."
    ),
)
async def verify_reset_otp(
    body: VerifyResetOTPRequest,
    db: AsyncSession = Depends(get_db),
) -> VerifyResetOTPResponse:
    result = await auth_service.verify_reset_otp(
        email=body.email,
        otp_code=body.otp_code,
        db=db,
    )
    return VerifyResetOTPResponse(**result)



@router.post(
    "/reset-password",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Step 3 — Set new password",
    description=(
        "Pass the reset_token from Step 2 as a Bearer token in the Authorization header. "
        "Body only needs new_password and confirm_password. "
        "The reset_token expires after 15 minutes."
    ),
)
async def reset_password(
    body: ResetPasswordRequest,
    credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    result = await auth_service.reset_password(
        reset_token=credentials.credentials,
        new_password=body.new_password,
        db=db,
    )
    return MessageResponse(message=result["message"])


@router.get(
    "/sample-documents",
    response_model=dict,
    status_code=status.HTTP_200_OK,
    summary="Get sample documents by category",
    description="Fetch platform sample documents filtered by business category for onboarding.",
)
async def get_sample_documents(
    category: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    documents = await auth_service.get_sample_documents_by_category(category, db)
    return {
        "message": "Sample documents retrieved successfully.",
        "documents": documents,
        "count": len(documents),
    }

