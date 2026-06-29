"""
Auth Service — all authentication business logic.

Signup flow (multi-step, matches Figma):
  Step 1: register_user() — create unverified user, send OTP
  Step 2: verify_email() — verify OTP, mark email verified
  Step 3: save_organization_info()— save business_category + employee_count_range
  Step 4: setup_workspace() — create tenant, link to user
  Step 5: select_plan() — create subscription + tenant_quota + usage_count row

Auth:
  signin() — validate credentials, return JWT pair
  refresh_tokens() — validate refresh token, return new JWT pair
  get_current_user() — FastAPI dependency to extract + validate access token
"""

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from app.config import settings
from app.core.email import send_otp_email
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_otp,
    hash_otp,
    hash_password,
    verify_otp,
    verify_password,
)
from app.database import get_db
from app.models.email_verification import EmailVerification, OTPPurpose
from app.models.subscription import BillingCycle, PlanName, Subscription, SubscriptionStatus
from app.models.tenant import Tenant
from app.models.tenant_quota import TenantQuota
from app.models.usage_count import UsageCount
from app.models.user import AuthProvider, User

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)

PLAN_QUOTAS: dict[PlanName, dict] = {
    PlanName.free: {
        "max_documents": 10,
        "max_file_size_mb": 15,
        "max_questions_per_month": 50,
    },
    PlanName.pro: {
        "max_documents": 100,
        "max_file_size_mb": 50,
        "max_questions_per_month": -1,
    },
    PlanName.pro_plus: {
        "max_documents": -1,
        "max_file_size_mb": -1,
        "max_questions_per_month": -1,
    },
}

def _slugify(text: str) -> str:
    """Convert workspace name to a URL-safe lowercase slug."""
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug

def _first_of_month() -> datetime:
    today = datetime.now(timezone.utc)
    return today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

async def register_user(
    company_name: str,
    email: str,
    password: str,
    db: AsyncSession,
) -> dict:
    """
    Create an unverified user record and send a 4-digit OTP to the email.
    Returns {"message": ..., "email": ...}
    """
    result = await db.execute(select(User).where(User.email == email))
    existing = result.scalar_one_or_none()

    if existing:
        if existing.is_email_verified:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this email already exists.",
            )
        await _invalidate_old_otps(existing.id, db)
        await _create_and_send_otp(existing, db)
        return {
            "message": "Account already registered but not verified. A new OTP has been sent.",
            "email": email,
        }

    placeholder_tenant = Tenant(
        workspace_name="__pending__",
        slug=f"pending-{uuid.uuid4().hex[:8]}",
    )
    db.add(placeholder_tenant)
    await db.flush()

    loop = asyncio.get_event_loop()
    pw_hash = await loop.run_in_executor(None, hash_password, password)

    user = User(
        tenant_id=placeholder_tenant.id,
        full_name=company_name.strip(),
        email=email.lower().strip(),
        password_hash=pw_hash,
        is_email_verified=False,
    )
    db.add(user)
    await db.flush()

    await _create_and_send_otp(user, db)

    return {
        "message": "Account created. Please check your email for the verification code.",
        "email": email,
    }

async def verify_email(
    email: str,
    otp: str,
    db: AsyncSession,
) -> dict:
    """
    Validate the OTP for the given email.
    Returns {"message": ..., "email": ...}
    """
    user = await _get_user_by_email_or_404(email, db)

    if user.is_email_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email is already verified.",
        )

    result = await db.execute(
        select(EmailVerification)
        .where(
            EmailVerification.user_id == user.id,
            EmailVerification.purpose == OTPPurpose.email_verification,
            EmailVerification.is_used == False,
            EmailVerification.expires_at > datetime.now(timezone.utc),
        )
        .order_by(EmailVerification.created_at.desc())
        .limit(1)
    )
    otp_record = result.scalar_one_or_none()

    if otp_record is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OTP has expired or does not exist. Please request a new one.",
        )

    if not verify_otp(otp, otp_record.otp_code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OTP code.",
        )

    otp_record.is_used = True
    user.is_email_verified = True

    onboarding_token = create_access_token(
        data={"sub": str(user.id), "purpose": "onboarding"},
        expires_delta=timedelta(hours=1),
    )

    return {
        "message": "Email verified successfully.",
        "email": email,
        "onboarding_token": onboarding_token,
    }

async def save_organization_info(
    user: User,
    business_category: str,
    employee_count_range: str,
    db: AsyncSession,
) -> dict:
    """
    Persist organisation info onto the tenant record.
    The tenant exists as a placeholder from Step 1.
    """
    try:
        tenant = await _get_tenant_for_user(user, db)

        tenant.business_category = business_category
        tenant.employee_count_range = employee_count_range
        await db.flush()
        await db.commit()

        return {
            "message": "Organisation info saved.",
            "email": user.email,
            "full_name": user.full_name,
            "business_category": business_category,
            "employee_count_range": employee_count_range,
        }
    except Exception as err:
        await db.rollback()
        logger.error(f"Failed to save organization info: {err}")
        raise

async def setup_workspace(
    user: User,
    workspace_name: str,
    db: AsyncSession,
) -> dict:
    """
    Set the tenant workspace name and generate its unique slug.
    """
    try:
        tenant = await _get_tenant_for_user(user, db)

        base_slug = _slugify(workspace_name)

        slug = base_slug
        counter = 1
        while True:
            result = await db.execute(
                select(Tenant).where(Tenant.slug == slug, Tenant.id != tenant.id)
            )
            if result.scalar_one_or_none() is None:
                break
            slug = f"{base_slug}-{counter}"
            counter += 1

        tenant.workspace_name = workspace_name.strip()
        tenant.slug = slug
        await db.flush()
        await db.commit()

        return {
            "message": "Workspace set up successfully.",
            "workspace_name": tenant.workspace_name,
            "slug": tenant.slug,
        }
    except Exception as err:
        await db.rollback()
        logger.error(f"Failed to setup workspace: {err}")
        raise

async def select_plan(
    user: User,
    plan_name: PlanName,
    billing_cycle: Optional[BillingCycle],
    db: AsyncSession,
) -> dict:
    """
    Create subscription + tenant_quota + initial usage_count row.
    This completes onboarding.
    """
    tenant = await _get_tenant_for_user(user, db)

    if plan_name != PlanName.free and billing_cycle is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="billing_cycle is required for paid plans.",
        )

    result = await db.execute(
        select(Subscription).where(Subscription.tenant_id == tenant.id)
    )
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Subscription already exists for this tenant.",
        )

    expires_at = None
    if plan_name != PlanName.free:
        delta = timedelta(days=30) if billing_cycle == BillingCycle.monthly else timedelta(days=365)
        expires_at = datetime.now(timezone.utc) + delta

    subscription = Subscription(
        tenant_id=tenant.id,
        plan_name=plan_name,
        billing_cycle=billing_cycle,
        status=SubscriptionStatus.active,
        expires_at=expires_at,
    )
    db.add(subscription)
    await db.flush()

    quotas = PLAN_QUOTAS[plan_name]
    tenant_quota = TenantQuota(
        tenant_id=tenant.id,
        subscription_id=subscription.id,
        **quotas,
    )
    db.add(tenant_quota)

    usage = UsageCount(
        tenant_id=tenant.id,
        period_month=_first_of_month().date(),
    )
    db.add(usage)
    await db.flush()

    tokens = _issue_tokens(user)

    return {
        "message": "Onboarding complete. Welcome to SecureRAG++!",
        **tokens,
    }

async def complete_onboarding(
    user: User,
    role: str,
    team_size: str,
    goal: str,
    workspace_name: str,
    plan_name: PlanName = PlanName.free,
    billing_cycle: Optional[BillingCycle] = None,
    db: AsyncSession = None,
) -> dict:
    """
    Consolidated onboarding: save role, team size, goal, workspace name,
    and create subscription in one call. Replaces steps 3, 4, 5.
    """
    tenant = await _get_tenant_for_user(user, db)

    tenant.employee_count_range = team_size
    tenant.workspace_name = workspace_name.strip()

    base_slug = _slugify(workspace_name)
    slug = base_slug
    counter = 1
    while True:
        result = await db.execute(
            select(Tenant).where(Tenant.slug == slug, Tenant.id != tenant.id)
        )
        if result.scalar_one_or_none() is None:
            break
        slug = f"{base_slug}-{counter}"
        counter += 1

    tenant.slug = slug

    if plan_name != PlanName.free and billing_cycle is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="billing_cycle is required for paid plans.",
        )

    result = await db.execute(
        select(Subscription).where(Subscription.tenant_id == tenant.id)
    )
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Subscription already exists for this tenant.",
        )

    expires_at = None
    if plan_name != PlanName.free:
        delta = timedelta(days=30) if billing_cycle == BillingCycle.monthly else timedelta(days=365)
        expires_at = datetime.now(timezone.utc) + delta

    subscription = Subscription(
        tenant_id=tenant.id,
        plan_name=plan_name,
        billing_cycle=billing_cycle,
        status=SubscriptionStatus.active,
        expires_at=expires_at,
    )
    db.add(subscription)
    await db.flush()

    quotas = PLAN_QUOTAS[plan_name]
    tenant_quota = TenantQuota(
        tenant_id=tenant.id,
        subscription_id=subscription.id,
        **quotas,
    )
    db.add(tenant_quota)

    usage = UsageCount(
        tenant_id=tenant.id,
        period_month=_first_of_month().date(),
    )
    db.add(usage)
    await db.flush()
    await db.commit()

    tokens = _issue_tokens(user)

    return {
        "message": "Onboarding complete. Welcome to SecureRAG++!",
        "workspace_name": tenant.workspace_name,
        "slug": tenant.slug,
        **tokens,
    }

async def google_login(token: str, db: AsyncSession) -> dict:
    """
    Verify a Google ID token and sign in (or register) the user.

    - If the user already exists with auth_provider=google, issue JWT tokens.
    - If the user exists with auth_provider=email (same email), link the
      Google account and issue tokens.
    - If no user exists, create a new user + placeholder tenant and issue
      an onboarding_token so the frontend can complete steps 3-5.
    """
    try:
        idinfo = google_id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            settings.GOOGLE_CLIENT_ID,
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Google ID token.",
        )

    google_uid: str = idinfo["sub"]
    email: str = idinfo.get("email", "").lower().strip()
    full_name: str = idinfo.get("name", email.split("@")[0])

    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google account does not have an email address.",
        )

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user is not None:
        if user.auth_provider == AuthProvider.email:
            user.auth_provider = AuthProvider.google
            user.provider_uid = google_uid
            user.is_email_verified = True

        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account is deactivated.",
            )

        sub_result = await db.execute(
            select(Subscription).where(Subscription.tenant_id == user.tenant_id)
        )
        has_subscription = sub_result.scalar_one_or_none() is not None

        if has_subscription:
            return {**_issue_tokens(user), "is_new_user": False}

        onboarding_token = create_access_token(
            data={"sub": str(user.id), "purpose": "onboarding"},
            expires_delta=timedelta(hours=1),
        )
        return {
            "access_token": "",
            "refresh_token": "",
            "token_type": "bearer",
            "is_new_user": True,
            "onboarding_token": onboarding_token,
        }

    placeholder_tenant = Tenant(
        workspace_name="__pending__",
        slug=f"pending-{uuid.uuid4().hex[:8]}",
    )
    db.add(placeholder_tenant)
    await db.flush()

    user = User(
        tenant_id=placeholder_tenant.id,
        full_name=full_name,
        email=email,
        password_hash=None,
        auth_provider=AuthProvider.google,
        provider_uid=google_uid,
        is_email_verified=True,
    )
    db.add(user)
    await db.flush()

    onboarding_token = create_access_token(
        data={"sub": str(user.id), "purpose": "onboarding"},
        expires_delta=timedelta(hours=1),
    )

    return {
        "access_token": "",
        "refresh_token": "",
        "token_type": "bearer",
        "is_new_user": True,
        "onboarding_token": onboarding_token,
    }

async def signin(
    email: str,
    password: str,
    db: AsyncSession,
) -> dict:
    result = await db.execute(
        select(User)
        .where(User.email == email.lower().strip())
        .options(selectinload(User.tenant))
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No account found with this email address.",
        )

    if not user.is_email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email not verified. Please verify your email first.",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated.",
        )

    if not verify_password(password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    if not user.tenant or not user.tenant.business_category:
        logger.info(f"User {user.id} ({user.email}) needs to complete onboarding")
        onboarding_token = create_access_token(
            data={"sub": str(user.id), "purpose": "onboarding"},
            expires_delta=timedelta(hours=1),
        )
        return {
            "onboarding_token": onboarding_token,
            "token_type": "bearer",
            "needs_onboarding": True,
        }

    logger.info(f"User {user.id} ({user.email}) signin successful")
    return _issue_tokens(user)

async def refresh_tokens(
    refresh_token: str,
    db: AsyncSession,
) -> dict:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired refresh token.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(refresh_token)
    except JWTError:
        raise credentials_error

    if payload.get("type") != "refresh":
        raise credentials_error

    user_id: Optional[str] = payload.get("sub")
    if not user_id:
        raise credentials_error

    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()

    if user is None or not user.is_active:
        raise credentials_error

    return _issue_tokens(user)

async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    FastAPI dependency — extracts and validates the Bearer access token.
    Inject this into any protected route.
    """
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if credentials is None:
        raise credentials_error

    try:
        payload = decode_token(credentials.credentials)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is invalid or has expired.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if payload.get("type") != "access":
        raise credentials_error

    user_id: Optional[str] = payload.get("sub")
    if not user_id:
        raise credentials_error

    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()

    if user is None:
        raise credentials_error

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated.",
        )

    return user

async def get_onboarding_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    FastAPI dependency for onboarding steps 3, 4, 5.
    Validates the short-lived onboarding token issued after OTP verification.
    """
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Valid onboarding token required.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if credentials is None:
        raise credentials_error

    try:
        payload = decode_token(credentials.credentials)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Onboarding token is invalid or has expired.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if payload.get("purpose") != "onboarding":
        raise credentials_error

    user_id: Optional[str] = payload.get("sub")
    if not user_id:
        raise credentials_error

    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()

    if user is None or not user.is_active:
        raise credentials_error

    return user

async def get_any_valid_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Accepts either an onboarding token (purpose=onboarding) or a regular
    access token (type=access). Use on endpoints that must work both during
    onboarding and after it is complete.
    """
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="A valid onboarding or access token is required.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if credentials is None:
        raise credentials_error

    try:
        payload = decode_token(credentials.credentials)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is invalid or has expired.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    is_onboarding = payload.get("purpose") == "onboarding"
    is_access = payload.get("type") == "access"

    if not is_onboarding and not is_access:
        raise credentials_error

    user_id: Optional[str] = payload.get("sub")
    if not user_id:
        raise credentials_error

    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()

    if user is None or not user.is_active:
        raise credentials_error

    return user

async def resend_otp(email: str, db: AsyncSession) -> dict:
    user = await _get_user_by_email_or_404(email, db)

    if user.is_email_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email is already verified.",
        )

    await _invalidate_old_otps(user.id, db, purpose=OTPPurpose.email_verification)
    await _create_and_send_otp(user, db, purpose=OTPPurpose.email_verification)

    return {
        "message": "A new OTP has been sent to your email.",
        "email": email,
    }

async def _create_and_send_otp(
    user: User,
    db: AsyncSession,
    purpose: OTPPurpose = OTPPurpose.email_verification,
) -> None:
    otp = generate_otp()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.OTP_EXPIRE_MINUTES)

    loop = asyncio.get_event_loop()
    hashed_otp = await loop.run_in_executor(None, hash_otp, otp)

    otp_record = EmailVerification(
        user_id=user.id,
        otp_code=hashed_otp,
        purpose=purpose,
        expires_at=expires_at,
        is_used=False,
    )
    db.add(otp_record)
    await db.flush()

    asyncio.create_task(send_otp_email(user.email, user.full_name, otp))

async def _invalidate_old_otps(
    user_id: uuid.UUID,
    db: AsyncSession,
    purpose: OTPPurpose = OTPPurpose.email_verification,
) -> None:
    result = await db.execute(
        select(EmailVerification).where(
            EmailVerification.user_id == user_id,
            EmailVerification.purpose == purpose,
            EmailVerification.is_used == False,
        )
    )
    for record in result.scalars().all():
        record.is_used = True

async def _get_user_by_email_or_404(email: str, db: AsyncSession) -> User:
    result = await db.execute(select(User).where(User.email == email.lower().strip()))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No account found with this email address.",
        )
    return user

async def _get_verified_user_or_401(email: str, db: AsyncSession) -> User:
    user = await _get_user_by_email_or_404(email, db)
    if not user.is_email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email is not verified. Complete Step 2 first.",
        )
    return user

async def _get_tenant_for_user(user: User, db: AsyncSession) -> Tenant:
    result = await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Tenant record not found.",
        )
    return tenant

def _issue_tokens(user: User) -> dict:
    payload = {"sub": str(user.id)}
    return {
        "access_token": create_access_token(payload),
        "refresh_token": create_refresh_token(payload),
        "token_type": "bearer",
    }

async def forgot_password(email: str, db: AsyncSession) -> dict:
    """
    Send a password-reset OTP to the given email.
    Always returns a success message even if the email is not found,
    to prevent user enumeration.
    """
    result = await db.execute(select(User).where(User.email == email.lower().strip()))
    user = result.scalar_one_or_none()

    if user is None or not user.is_active:
        return {
            "message": "If an account with that email exists, a reset code has been sent.",
            "email": email,
        }

    await _invalidate_old_otps(user.id, db, purpose=OTPPurpose.password_reset)
    await _create_and_send_otp(user, db, purpose=OTPPurpose.password_reset)

    return {
        "message": "If an account with that email exists, a reset code has been sent.",
        "email": email,
    }

async def verify_reset_otp(email: str, otp: str, db: AsyncSession) -> dict:
    """
    Validates the password-reset OTP.
    On success returns a short-lived reset token the client must send
    back with the new password.
    """
    user = await _get_user_by_email_or_404(email, db)

    result = await db.execute(
        select(EmailVerification)
        .where(
            EmailVerification.user_id == user.id,
            EmailVerification.purpose == OTPPurpose.password_reset,
            EmailVerification.is_used == False,
            EmailVerification.expires_at > datetime.now(timezone.utc),
        )
        .order_by(EmailVerification.created_at.desc())
        .limit(1)
    )
    otp_record = result.scalar_one_or_none()

    if otp_record is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reset code has expired or does not exist. Please request a new one.",
        )

    if not verify_otp(otp, otp_record.otp_code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid reset code.",
        )

    otp_record.is_used = True

    reset_token = create_access_token(
        data={"sub": str(user.id), "purpose": "password_reset"},
        expires_delta=timedelta(minutes=15),
    )

    return {
        "message": "OTP verified. Use the reset_token to set your new password.",
        "reset_token": reset_token,
    }

async def reset_password(
    email: str,
    otp: str,
    new_password: str,
    db: AsyncSession,
) -> dict:
    """
    Reset password with email, OTP, and new password in a single call.
    """
    user = await _get_user_by_email_or_404(email, db)

    result = await db.execute(
        select(EmailVerification)
        .where(
            EmailVerification.user_id == user.id,
            EmailVerification.purpose == OTPPurpose.password_reset,
            EmailVerification.is_used == False,
            EmailVerification.expires_at > datetime.now(timezone.utc),
        )
        .order_by(EmailVerification.created_at.desc())
        .limit(1)
    )
    otp_record = result.scalar_one_or_none()

    if otp_record is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reset code has expired or does not exist. Please request a new one.",
        )

    if not verify_otp(otp, otp_record.otp_code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid reset code.",
        )

    otp_record.is_used = True

    loop = asyncio.get_event_loop()
    pw_hash = await loop.run_in_executor(None, hash_password, new_password)
    user.password_hash = pw_hash
    await db.commit()

    return {"message": "Password reset successfully. You can now sign in with your new password."}

async def get_sample_documents_by_category(
    business_category: str,
    db: AsyncSession,
) -> list[dict]:
    """
    Fetch sample documents from the database for the given business category.
    Used during onboarding when user selects "show sample documents".
    """
    from app.models.sample_document import SampleDocument

    result = await db.execute(
        select(SampleDocument)
        .where(SampleDocument.business_category == business_category)
        .where(SampleDocument.is_active == True)
    )
    documents = result.scalars().all()

    return [
        {
            "id": str(doc.id),
            "title": doc.title,
            "desc": doc.description,
            "pages": 0,
            "filename": doc.filename,
            "file_size_mb": doc.file_size_mb,
        }
        for doc in documents
    ]
