"""
Frontend-compat user endpoints (/api/user/...).

  - GET /api/user/me       -> User (id, email, companyName, onboardingCompleted)
  - PUT /api/user/password -> {success}   (Profile page — change own password)
  - GET /api/user/profile  -> UserProfile (name, email, companyName, avatarUrl, role)
  - PUT /api/user/profile  -> {success, profile}
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.frontend import helpers
from app.core.rate_limit import limiter
from app.core.security import create_access_token, hash_password, verify_password
from app.database import get_db
from app.models.user import User
from app.services.auth_service import invalidate_user_cache
from app.schemas.frontend import (
    FEChangePasswordRequest,
    FEChangePasswordResponse,
    FESaveProfileRequest,
    FESaveProfileResponse,
    FESuccessResponse,
    FEUser,
    FEUserProfile,
)
from app.services.auth_service import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/user", tags=["Frontend — User"])

MAX_AVATAR_MB = 2
ALLOWED_AVATAR_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

class FEAvatarUploadResponse(BaseModel):
    avatarUrl: str

@router.post("/avatar", response_model=FEAvatarUploadResponse)
@limiter.limit("10/minute")
async def upload_profile_photo(
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEAvatarUploadResponse:
    """Upload the user's profile photo to Cloudinary and save the hosted URL."""
    from app.core.storage import upload_image_to_cloudinary

    if file.content_type not in ALLOWED_AVATAR_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Photo must be a PNG, JPG, WebP, or GIF image.",
        )

    content = await file.read()
    if len(content) > MAX_AVATAR_MB * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Photo must be at most {MAX_AVATAR_MB}MB.",
        )

    try:
        avatar_url = await upload_image_to_cloudinary(content, current_user.id, kind="profile")
    except Exception as e:
        logger.exception("Profile photo upload failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Image upload failed: {e}",
        )

    current_user.avatar_url = avatar_url
    await db.flush()
    invalidate_user_cache(current_user.id)
    return FEAvatarUploadResponse(avatarUrl=avatar_url)

async def _profile(user: User, db: AsyncSession) -> FEUserProfile:
    tenant = await helpers.get_tenant(user, db)
    company = tenant.workspace_name if tenant.workspace_name != "__pending__" else user.full_name
    return FEUserProfile(
        name=user.full_name,
        email=user.email,
        companyName=company,
        avatarUrl=user.avatar_url,
        role=(await helpers.role_for(user, db)).capitalize(),
    )

@router.get("/me", response_model=FEUser)
async def get_me(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEUser:
    tenant, subscription = await helpers.get_tenant_and_subscription(current_user, db)
    company = tenant.workspace_name if tenant.workspace_name != "__pending__" else current_user.full_name
    return FEUser(
        id=str(current_user.id),
        email=current_user.email,
        companyName=company,
        onboardingCompleted=helpers.onboarding_completed(tenant),
        role=await helpers.role_for(current_user, db),
    )


async def _audit_password(request: Request, user: User, outcome: str, **details) -> None:
    from app.core.audit_middleware import client_ip
    from app.services import audit_service

    await audit_service.record(
        action="user.password.change", outcome=outcome, tenant_id=user.tenant_id, actor_user_id=user.id,
        actor_email=user.email, resource_type="user", resource_id=str(user.id), method=request.method,
        path=request.url.path, ip=client_ip(request), user_agent=request.headers.get("user-agent"),
        details=details or None,
    )

@router.put("/password", response_model=FEChangePasswordResponse)
@limiter.limit("5/minute")
async def change_password(
    request: Request,
    body: FEChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FESuccessResponse:
    if not current_user.password_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This account uses Google sign-in and has no password.",
        )
    loop = asyncio.get_event_loop()
    # Offload bcrypt verify to a thread — it blocks the event loop otherwise
    # (this endpoint previously verified synchronously).
    valid = await loop.run_in_executor(
        None, verify_password, body.currentPassword, current_user.password_hash
    )
    if not valid:
        await _audit_password(request, current_user, "failure", reason="current_password_incorrect")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect.",
        )

    current_user.password_hash = await loop.run_in_executor(None, hash_password, body.newPassword)
    await _audit_password(request, current_user, "success")
    # Invalidate every existing session for this user — a password change must
    # log out other devices...
    current_user.token_version = (current_user.token_version or 0) + 1
    await db.flush()
    invalidate_user_cache(current_user.id)
    # ...but not THIS one: hand back a token minted under the new version so
    # the device that changed the password stays signed in (previously its
    # next request 401'd and the app hard-redirected to "/").
    token = create_access_token(
        {"sub": str(current_user.id), "tenant_id": str(current_user.tenant_id), "tv": current_user.token_version}
    )
    return FEChangePasswordResponse(success=True, token=token)

@router.get("/profile", response_model=FEUserProfile)
async def get_profile(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEUserProfile:
    return await _profile(current_user, db)

@router.put("/profile", response_model=FESaveProfileResponse)
async def save_profile(
    body: FESaveProfileRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FESaveProfileResponse:
    new_email = body.email.lower().strip()
    if new_email != current_user.email:
        # The email is the login identity and the only thing that was ever
        # verified (OTP at signup). Letting a session silently swap it to an
        # unverified address — with no OTP to the new one and no notice to
        # the old — is an account-takeover vector, so it's refused until a
        # proper re-verification flow exists. The Profile page shows the
        # field read-only for the same reason.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Changing your email address isn't supported yet. Contact support to update it.",
        )

    current_user.full_name = body.name.strip()
    # Distinguish "field omitted" (leave unchanged) from an explicit null
    # (the Remove button clearing the photo).
    if "avatarUrl" in body.model_fields_set:
        current_user.avatar_url = body.avatarUrl

    tenant = await helpers.get_tenant(current_user, db)
    if body.companyName.strip():
        tenant.workspace_name = body.companyName.strip()

    await db.flush()
    invalidate_user_cache(current_user.id)
    return FESaveProfileResponse(success=True, profile=await _profile(current_user, db))
