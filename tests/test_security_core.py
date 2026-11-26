"""Unit tests: password hashing, JWT creation/decoding, OTP helpers (app.core.security)."""
import time
from datetime import timedelta

import pytest
from jose import JWTError, jwt

from app.config import settings
from app.core import security


def test_hash_is_salted_and_verifies():
    h1, h2 = security.hash_password("Passw0rd!"), security.hash_password("Passw0rd!")
    assert h1 != h2                       # per-hash salt
    assert security.verify_password("Passw0rd!", h1)
    assert security.verify_password("Passw0rd!", h2)


def test_wrong_password_rejected():
    assert not security.verify_password("wrong", security.hash_password("right"))


def test_plaintext_never_in_hash():
    assert "Secret123" not in security.hash_password("Secret123")


def test_access_token_claims():
    tok = security.create_access_token({"sub": "u1", "tenant_id": "t1"})
    claims = security.decode_token(tok)
    assert claims["sub"] == "u1" and claims["tenant_id"] == "t1"
    assert claims["type"] == "access" and claims["jti"] and claims["exp"]


def test_refresh_token_returns_jti_and_expiry():
    tok, jti, expire = security.create_refresh_token({"sub": "u1"})
    claims = security.decode_token(tok)
    assert claims["type"] == "refresh" and claims["jti"] == jti
    assert expire.tzinfo is not None


def test_each_token_has_unique_jti():
    a = security.decode_token(security.create_access_token({"sub": "u"}))
    b = security.decode_token(security.create_access_token({"sub": "u"}))
    assert a["jti"] != b["jti"]


def test_tampered_token_rejected():
    tok = security.create_access_token({"sub": "u1"})
    head, body, sig = tok.split(".")
    with pytest.raises(JWTError):
        security.decode_token(f"{head}.{body}.{sig[:-2]}xx")


def test_token_signed_with_other_key_rejected():
    forged = jwt.encode({"sub": "u1"}, "attacker-key", algorithm=settings.ALGORITHM)
    with pytest.raises(JWTError):
        security.decode_token(forged)


def test_expired_token_rejected():
    tok = security.create_access_token({"sub": "u1"}, expires_delta=timedelta(seconds=-5))
    with pytest.raises(JWTError):
        security.decode_token(tok)


def test_garbage_token_rejected():
    with pytest.raises(JWTError):
        security.decode_token("not.a.jwt")


def test_otp_format_and_hash_roundtrip():
    otp = security.generate_otp()
    assert len(otp) == 6 and otp.isdigit()
    h = security.hash_otp(otp)
    assert security.verify_otp(otp, h)
    assert not security.verify_otp("000000" if otp != "000000" else "111111", h)


def test_otp_is_not_constant():
    assert len({security.generate_otp() for _ in range(20)}) > 1
