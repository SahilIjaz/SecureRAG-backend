"""
Auth Service — all authentication business logic.

register_user() is Step 1 of signup (create unverified user, send OTP); the
frontend-compat routes in api/frontend/auth.py handle OTP verification and
login directly, and api/frontend/onboarding.py owns steps 2+ of onboarding —
neither goes through this module beyond that.

get_current_user() — FastAPI dependency to extract + validate access token.
"""

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import make_transient_to_detached

from app.config import settings
from app.core.email import send_otp_email
from app.core import perf_timing
from app.core.entitlements import PLAN_ENTITLEMENTS
from app.core.security import (
    create_access_token,
    decode_token,
    generate_otp,
    hash_otp,
    hash_password,
    verify_otp,
)
from app.database import get_db
from app.models.email_verification import EmailVerification, OTPPurpose
from app.models.subscription import PlanName
from app.models.tenant import Tenant
from app.models.user import User
from app.models.revoked_token import RevokedToken

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)

# Backwards-compatible view over the single source of truth in
# app/core/entitlements.py. Kept as a dict keyed by PlanName so the existing
# `PLAN_QUOTAS[plan]["max_documents"]` call sites keep working; new code should
# prefer entitlements.get_entitlements(subscription) directly.
PLAN_QUOTAS: dict[PlanName, dict] = {
    plan: ent.quota_dict() for plan, ent in PLAN_ENTITLEMENTS.items()
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

# ── Short-TTL user cache ────────────────────────────────────────────────────
#
# get_current_user/get_onboarding_user/get_any_valid_user run on *every*
# authenticated request and each used to do their own `SELECT * FROM users
# WHERE id = ...` — on a remote Postgres that's 250ms-3.5s paid again and
# again for a row that rarely changes between two calls a few seconds apart.
#
# Caching the live ORM `User` instance itself would be wrong: several
# endpoints (settings.py deactivate_workspace, user.py avatar/password/
# profile updates) mutate `current_user.<field>` directly and rely on the
# request's own session to flush that write — a stale object from a
# *different* request's session wouldn't be tracked by this request's
# session, so the mutation would silently never be saved.
#
# Session.merge(obj, load=False) is SQLAlchemy's documented mechanism for
# exactly this ("used for cache population... foregoes all database access"):
# given a transient copy of the cached column values, it splices them into
# THIS request's session as a fully tracked, persistent instance — no SELECT,
# and any subsequent mutation on the returned object still flushes normally.
_USER_CACHE_COLUMNS = (
    "id", "tenant_id", "full_name", "email", "password_hash",
    "auth_provider", "provider_uid", "avatar_url",
    "is_email_verified", "is_active", "token_version", "created_at", "updated_at",
)
_user_cache: dict[str, tuple[float, dict]] = {}

def _cache_user(user: User) -> None:
    data = {col: getattr(user, col) for col in _USER_CACHE_COLUMNS}
    _user_cache[str(user.id)] = (time.monotonic() + settings.AUTH_USER_CACHE_TTL_SECONDS, data)

def invalidate_user_cache(user_id) -> None:
    """Call after any direct `current_user.<field> = ...` mutation so the
    next request sees it immediately instead of waiting out the TTL."""
    _user_cache.pop(str(user_id), None)

async def _load_user(user_id: str, db: AsyncSession) -> Optional[User]:
    cached = _user_cache.get(user_id)
    if cached is not None and cached[0] > time.monotonic():
        transient = User(**cached[1])
        # merge(..., load=False) refuses a plain transient object (it has no
        # identity key yet) — make_transient_to_detached is SQLAlchemy's own
        # helper for exactly this "populate the session from an in-memory
        # cache, skip the SELECT" case: it synthesizes the identity key from
        # the primary key we already set, without touching the DB.
        make_transient_to_detached(transient)
        return await db.merge(transient, load=False)

    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if user is not None:
        _cache_user(user)
    return user

async def register_user(
    company_name: str,
    email: str,
    password: str,
    db: AsyncSession,
) -> dict:
    """
    Create an unverified user record and send a 6-digit OTP to the email.
    Returns {"message": ..., "email": ...}
    """
    with perf_timing.timed("existing_user_lookup"):
        result = await db.execute(select(User).where(User.email == email))
        existing = result.scalar_one_or_none()

    if existing:
        if existing.is_email_verified:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this email already exists.",
            )
        with perf_timing.timed("invalidate_old_otps"):
            await _invalidate_old_otps(existing.id, db)
        await _create_and_send_otp(existing, db)
        return {
            "message": "Account already registered but not verified. A new OTP has been sent.",
            "email": email,
        }

    # Tenant.id/User.id use an ORM-side `default=uuid.uuid4` — that default is
    # only applied by SQLAlchemy at flush time, NOT at object construction, so
    # reading placeholder_tenant.id / user.id here before any flush would
    # silently be None (confirmed: this previously inserted a NULL
    # users.tenant_id and crashed signup with an IntegrityError). Generating
    # the UUIDs ourselves up front avoids that trap while still needing only
    # the one flush _create_and_send_otp already does below (which then also
    # covers the OTP row), instead of an extra intermediate round trip.
    tenant_id = uuid.uuid4()
    placeholder_tenant = Tenant(
        id=tenant_id,
        workspace_name="__pending__",
        slug=f"pending-{uuid.uuid4().hex[:8]}",
    )
    db.add(placeholder_tenant)

    loop = asyncio.get_event_loop()
    with perf_timing.timed("bcrypt_hash_password"):
        pw_hash = await loop.run_in_executor(None, hash_password, password)

    user = User(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        full_name=company_name.strip(),
        email=email.lower().strip(),
        password_hash=pw_hash,
        is_email_verified=False,
    )
    db.add(user)
    # RBAC: the account creator owns the workspace.
    from app.models.tenant_user import TenantUser, TenantRole
    db.add(TenantUser(tenant_id=tenant_id, user_id=user.id, role=TenantRole.owner))

    await _create_and_send_otp(user, db)

    return {
        "message": "Account created. Please check your email for the verification code.",
        "email": email,
    }

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

    jti = payload.get("jti")
    if jti and await _is_jti_revoked(jti, db):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="You have been logged out. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id: Optional[str] = payload.get("sub")
    if not user_id:
        raise credentials_error

    with perf_timing.timed("auth_dep_load_user"):
        user = await _load_user(user_id, db)

    if user is None:
        raise credentials_error

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated.",
        )

    # Session revocation on password change: the token embeds the token_version
    # it was minted under. A password change bumps user.token_version, so every
    # token issued before it (missing claim => 0) no longer matches and is
    # rejected. (Bounded by AUTH_USER_CACHE_TTL_SECONDS unless the cache is
    # invalidated on change — see invalidate_user_cache.)
    if payload.get("tv", 0) != (user.token_version or 0):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your session has ended. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user

async def _is_jti_revoked(jti: str, db: AsyncSession) -> bool:
    result = await db.execute(select(RevokedToken.id).where(RevokedToken.jti == jti))
    return result.scalar_one_or_none() is not None

async def revoke_current_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    FastAPI dependency for POST /auth/logout — records the presented access
    token's jti as revoked so get_current_user() rejects it immediately
    instead of trusting it until its natural expiry. Deliberately lenient:
    a missing, malformed, wrong-type, or already-expired token is a no-op
    rather than a 401 — from the client's point of view logout always
    "succeeds", since there's nothing left worth revoking in those cases.
    """
    if credentials is None:
        return
    try:
        payload = decode_token(credentials.credentials)
    except JWTError:
        return
    if payload.get("type") != "access":
        return
    jti = payload.get("jti")
    exp = payload.get("exp")
    if not jti or not exp:
        return

    db.add(RevokedToken(jti=jti, expires_at=datetime.fromtimestamp(exp, tz=timezone.utc)))
    # Opportunistic cleanup so this table stays tiny without a separate
    # scheduled job — access tokens are short-lived (30 min by default), so
    # any row here is safe to drop once its own token would've expired anyway.
    await db.execute(delete(RevokedToken).where(RevokedToken.expires_at < datetime.now(timezone.utc)))

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

    with perf_timing.timed("auth_dep_load_user"):
        user = await _load_user(user_id, db)

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

    with perf_timing.timed("auth_dep_load_user"):
        user = await _load_user(user_id, db)

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
    with perf_timing.timed("bcrypt_hash_otp"):
        hashed_otp = await loop.run_in_executor(None, hash_otp, otp)

    otp_record = EmailVerification(
        user_id=user.id,
        otp_code=hashed_otp,
        purpose=purpose,
        expires_at=expires_at,
        is_used=False,
    )
    db.add(otp_record)
    with perf_timing.timed("db_flush_pending_rows"):
        await db.flush()

    if settings.DEBUG:
        # TEMP (latency audit): surface the plaintext OTP so the flow can be
        # driven end-to-end without depending on real email delivery time.
        # DEBUG-gated only — never logs in a non-DEBUG environment.
        logger.info("[DEV OTP] %s purpose=%s otp=%s", user.email, purpose.value, otp)

    async def _send_and_report() -> None:
        t0 = time.perf_counter()
        await send_otp_email(user.email, user.full_name, otp)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "[PERF][background, not counted in response time] send_otp_email user=%s elapsed=%.2fms",
            user.email, elapsed_ms,
        )

    asyncio.create_task(_send_and_report())

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

async def forgot_password(email: str, db: AsyncSession) -> dict:
    """
    Send a password-reset OTP to the given email.
    Raises 404 if no account exists for the email, matching reset_password's
    behavior (chosen over the enumeration-safe generic response).
    """
    result = await db.execute(select(User).where(User.email == email.lower().strip()))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No account found with this email address.")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account is inactive.")

    await _invalidate_old_otps(user.id, db, purpose=OTPPurpose.password_reset)
    await _create_and_send_otp(user, db, purpose=OTPPurpose.password_reset)

    return {
        "message": "A reset code has been sent to your email.",
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
    # Invalidate every existing session — a reset must log out all devices.
    user.token_version = (user.token_version or 0) + 1
    await db.commit()
    invalidate_user_cache(user.id)

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
