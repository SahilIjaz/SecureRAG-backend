"""
Frontend-compat auth endpoints (/api/auth/...).

The Nexus frontend (src/api/auth.api.ts) speaks a slightly different dialect
than the original /api/v1/auth routes:
  - POST /api/auth/signup {email, password, companyName} -> {success, message}
  - POST /api/auth/login {email, password} -> {success, token, user}
  - POST /api/auth/verify-otp {email, otp} -> {success, token?, user?}
      (used by BOTH the signup flow and the forgot-password flow)
  - POST /api/auth/forgot-password {email} -> {success}
  - POST /api/auth/reset-password {email, otp, newPassword} -> {success}

The frontend stores exactly ONE token (`res.token`) and sends it as the
Bearer token for every later call — so these routes always issue a real
ACCESS token, never the short-lived onboarding token from the v1 flow.
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.frontend import helpers
from app.core.rate_limit import limiter
from app.core.security import create_access_token, hash_password, verify_otp, verify_password
from app.database import get_db
from app.models.email_verification import EmailVerification, OTPPurpose
from app.models.user import User
from app.schemas.frontend import (
    FEForgotPasswordRequest,
    FELoginRequest,
    FELoginResponse,
    FEOtpVerifyRequest,
    FEOtpVerifyResponse,
    FEResetPasswordRequest,
    FESignupRequest,
    FESignupResponse,
    FESuccessResponse,
    FEUser,
)
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["Frontend — Auth"])

async def _fe_user(user: User, db: AsyncSession) -> FEUser:
    tenant = await helpers.get_tenant(user, db)
    subscription = await helpers.get_subscription(user.tenant_id, db)
    company = tenant.workspace_name if tenant.workspace_name != "__pending__" else user.full_name
    return FEUser(
        id=str(user.id),
        email=user.email,
        companyName=company,
        onboardingCompleted=helpers.onboarding_completed(subscription),
    )

@router.post("/signup", response_model=FESignupResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("3/minute")
async def signup(
    request: Request,
    body: FESignupRequest,
    db: AsyncSession = Depends(get_db),
) -> FESignupResponse:
    result = await auth_service.register_user(
        company_name=body.companyName,
        email=body.email,
        password=body.password,
        db=db,
    )
    return FESignupResponse(success=True, message=result["message"])

@router.post("/login", response_model=FELoginResponse)
@limiter.limit("5/minute")
async def login(
    request: Request,
    body: FELoginRequest,
    db: AsyncSession = Depends(get_db),
) -> FELoginResponse:
    result = await db.execute(select(User).where(User.email == body.email.lower().strip()))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No account found with this email address.")
    if not user.is_email_verified:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Email not verified. Please verify your email first.")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is deactivated.")
    if not user.password_hash or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password.")

    token = create_access_token({"sub": str(user.id), "tenant_id": str(user.tenant_id)})
    return FELoginResponse(success=True, token=token, user=await _fe_user(user, db))

async def _latest_valid_otp(
    user_id, purpose: OTPPurpose, db: AsyncSession
) -> Optional[EmailVerification]:
    result = await db.execute(
        select(EmailVerification)
        .where(
            EmailVerification.user_id == user_id,
            EmailVerification.purpose == purpose,
            EmailVerification.is_used == False,
            EmailVerification.expires_at > datetime.now(timezone.utc),
        )
        .order_by(EmailVerification.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()

@router.post("/verify-otp", response_model=FEOtpVerifyResponse)
@limiter.limit("5/minute")
async def verify_otp_endpoint(
    request: Request,
    body: FEOtpVerifyRequest,
    db: AsyncSession = Depends(get_db),
) -> FEOtpVerifyResponse:
    """
    Dual-purpose OTP check, mirroring the frontend's single OtpForm:
      - Unverified account  -> signup email verification: marks the email verified
        and returns a session token + user (the frontend calls setSession()).
      - Verified account    -> forgot-password flow: validates the reset OTP but
        does NOT consume it (POST /reset-password re-validates the same code),
        and returns no token.
    """
    result = await db.execute(select(User).where(User.email == body.email.lower().strip()))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No account found with this email address.")

    if not user.is_email_verified:
        otp_record = await _latest_valid_otp(user.id, OTPPurpose.email_verification, db)
        if otp_record is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OTP has expired or does not exist. Please request a new one.")
        if not verify_otp(body.otp, otp_record.otp_code):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OTP code.")

        otp_record.is_used = True
        user.is_email_verified = True
        await db.flush()

        token = create_access_token({"sub": str(user.id), "tenant_id": str(user.tenant_id)})
        return FEOtpVerifyResponse(success=True, token=token, user=await _fe_user(user, db))

    # Verified account — this is the forgot-password flow.
    otp_record = await _latest_valid_otp(user.id, OTPPurpose.password_reset, db)
    if otp_record is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reset code has expired or does not exist. Please request a new one.")
    if not verify_otp(body.otp, otp_record.otp_code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid reset code.")

    # Deliberately NOT marked used — /reset-password consumes it.
    return FEOtpVerifyResponse(success=True)

@router.post("/forgot-password", response_model=FESuccessResponse)
@limiter.limit("3/minute")
async def forgot_password(
    request: Request,
    body: FEForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> FESuccessResponse:
    await auth_service.forgot_password(email=body.email, db=db)
    return FESuccessResponse()

@router.post("/reset-password", response_model=FESuccessResponse)
@limiter.limit("5/minute")
async def reset_password(
    request: Request,
    body: FEResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> FESuccessResponse:
    result = await db.execute(select(User).where(User.email == body.email.lower().strip()))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No account found with this email address.")

    otp_record = await _latest_valid_otp(user.id, OTPPurpose.password_reset, db)
    if otp_record is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reset code has expired or does not exist. Please request a new one.")
    if not verify_otp(body.otp, otp_record.otp_code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid reset code.")

    otp_record.is_used = True
    loop = asyncio.get_event_loop()
    user.password_hash = await loop.run_in_executor(None, hash_password, body.newPassword)
    await db.flush()
    return FESuccessResponse()
