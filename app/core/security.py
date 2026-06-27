import random
import string
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=10)

def hash_password(password: str) -> str:
    """Return a bcrypt hash of *password*."""
    return _pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Return True if *plain_password* matches the stored *hashed_password*."""
    return _pwd_context.verify(plain_password, hashed_password)

def create_access_token(
    data: Dict[str, Any],
    expires_delta: Optional[timedelta] = None,
) -> str:
    """
    Create a signed JWT access token.

    Args:
        data: Payload claims to embed (must include a ``sub`` field).
        expires_delta: Custom lifetime. Defaults to ACCESS_TOKEN_EXPIRE_MINUTES.

    Returns:
        Encoded JWT string.
    """
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

def create_refresh_token(data: Dict[str, Any]) -> str:
    """
    Create a signed JWT refresh token with a longer lifetime.

    Args:
        data: Payload claims to embed (must include a ``sub`` field).

    Returns:
        Encoded JWT string.
    """
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

def decode_token(token: str) -> Dict[str, Any]:
    """
    Decode and validate a JWT token.

    Args:
        token: Raw JWT string.

    Returns:
        The decoded claims dictionary.

    Raises:
        jose.JWTError: If the token is invalid or expired.
    """
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])

_otp_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=6)

def generate_otp() -> str:
    """Return a random 4-digit OTP string (zero-padded, e.g. '0391')."""
    return "".join(random.choices(string.digits, k=4))

def hash_otp(otp: str) -> str:
    """Return a bcrypt hash of the plain OTP code for safe storage."""
    return _otp_context.hash(otp)

def verify_otp(plain_otp: str, hashed_otp: str) -> bool:
    """Return True if *plain_otp* matches the stored *hashed_otp*."""
    return _otp_context.verify(plain_otp, hashed_otp)
