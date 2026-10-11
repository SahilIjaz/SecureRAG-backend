"""
Post-quantum cryptography layer (NIST FIPS 203 / 204).

Why this exists: the platform's asymmetric-crypto surfaces are vulnerable to a
future quantum adversary running Shor's algorithm, and encrypted data stolen
today can simply be stored until such hardware exists ("harvest now, decrypt
later"). This module hardens the two surfaces this codebase owns end to end:

  1. Files at rest  — hybrid envelope encryption. Each file gets a fresh
     symmetric key; that key is wrapped under a key derived from an
     ML-KEM-768 (Kyber, FIPS 203) encapsulation against the platform's
     public key. Stealing the Cloudinary bucket AND the legacy Fernet key
     is no longer enough to read new files — the ML-KEM private key is also
     required, and ML-KEM is believed quantum-resistant.

  2. Widget session tokens — an ML-DSA-44 (Dilithium, FIPS 204) signature is
     appended to the existing HMAC-signed JWT (hybrid: both must verify), so
     token forgery stays infeasible even against a quantum attacker.

Design constraints honored here:
  - Hybrid, never pure-PQ: the classical layer (Fernet / HS256) always remains,
    so security can only be added, never regressed, by this module.
  - Zero schema impact: the PQ envelope lives inside the stored blob itself
    (magic-prefixed), and legacy blobs/tokens keep decrypting/verifying —
    rollout and rollback are both a config flag.
  - Pure-Python implementations (kyber-py / dilithium-py) keep the dependency
    footprint trivial; both are CPU-bound, so async callers run them via
    asyncio.to_thread. Swap in liboqs bindings later for raw speed without
    touching this module's interface.
"""

from __future__ import annotations

import base64
import logging
import struct
from typing import Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import settings

logger = logging.getLogger(__name__)

# Blob layout: MAGIC | u32 len(kem_ct) | kem_ct | u32 len(wrapped_key) |
# wrapped_key | fernet_ciphertext. The magic cannot collide with legacy
# content: Fernet ciphertexts are urlsafe-base64 text starting with "gAAAA".
PQ_MAGIC = b"NXPQ1"

# Domain-separation label for the HKDF that turns the 32-byte ML-KEM shared
# secret into the key-wrapping key — standard practice so the same shared
# secret could never be reused for an unrelated purpose with the same bytes.
_HKDF_INFO = b"nexus/pq/file-key-wrap/v1"

# Separator between the classical JWT and the appended ML-DSA signature in a
# PQ-signed widget session token. '~' never appears in base64url or JWT dots.
PQ_TOKEN_SEPARATOR = "~"


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(encoded: str) -> bytes:
    return base64.b64decode(encoded.encode("ascii"))


# ── Key generation (used by scripts/generate_pq_keys.py) ────────────────────

def generate_kem_keypair() -> Tuple[str, str]:
    """Fresh ML-KEM-768 keypair as (public_b64, private_b64)."""
    from kyber_py.ml_kem import ML_KEM_768

    ek, dk = ML_KEM_768.keygen()
    return _b64e(ek), _b64e(dk)


def generate_sig_keypair() -> Tuple[str, str]:
    """Fresh ML-DSA-44 keypair as (public_b64, private_b64)."""
    from dilithium_py.ml_dsa import ML_DSA_44

    pk, sk = ML_DSA_44.keygen()
    return _b64e(pk), _b64e(sk)


# ── Readiness checks (config flag AND keys present) ─────────────────────────

def file_encryption_ready() -> bool:
    return bool(
        settings.PQ_FILE_ENCRYPTION_ENABLED
        and settings.PQ_KEM_PUBLIC_KEY
        and settings.PQ_KEM_PRIVATE_KEY
    )


def token_signing_ready() -> bool:
    return bool(
        settings.PQ_WIDGET_TOKEN_SIGNING_ENABLED
        and settings.PQ_SIG_PUBLIC_KEY
        and settings.PQ_SIG_PRIVATE_KEY
    )


# ── Hybrid file encryption (ML-KEM-768 envelope around per-file Fernet) ─────

def is_pq_blob(content: bytes) -> bool:
    return content[: len(PQ_MAGIC)] == PQ_MAGIC


def _derive_wrap_cipher(shared_secret: bytes) -> Fernet:
    kek = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO
    ).derive(shared_secret)
    return Fernet(base64.urlsafe_b64encode(kek))


def hybrid_encrypt(content: bytes) -> bytes:
    """Envelope-encrypt `content` for the platform's ML-KEM-768 public key.

    CPU-bound (pure-Python Kyber): async callers should run this via
    asyncio.to_thread. Raises if PQ file encryption is not configured —
    callers are expected to gate on file_encryption_ready().
    """
    from kyber_py.ml_kem import ML_KEM_768

    if not settings.PQ_KEM_PUBLIC_KEY:
        raise ValueError("PQ_KEM_PUBLIC_KEY not configured")

    file_key = Fernet.generate_key()
    ciphertext = Fernet(file_key).encrypt(content)

    shared_secret, kem_ct = ML_KEM_768.encaps(_b64d(settings.PQ_KEM_PUBLIC_KEY))
    wrapped_key = _derive_wrap_cipher(shared_secret).encrypt(file_key)

    return b"".join([
        PQ_MAGIC,
        struct.pack(">I", len(kem_ct)), kem_ct,
        struct.pack(">I", len(wrapped_key)), wrapped_key,
        ciphertext,
    ])


def hybrid_decrypt(blob: bytes) -> bytes:
    """Inverse of hybrid_encrypt. Raises ValueError on any malformed or
    tampered blob (never returns partial plaintext)."""
    from kyber_py.ml_kem import ML_KEM_768

    if not settings.PQ_KEM_PRIVATE_KEY:
        raise ValueError("PQ_KEM_PRIVATE_KEY not configured")
    if not is_pq_blob(blob):
        raise ValueError("not a PQ envelope blob")

    offset = len(PQ_MAGIC)
    try:
        (kem_len,) = struct.unpack_from(">I", blob, offset)
        offset += 4
        kem_ct = blob[offset : offset + kem_len]
        offset += kem_len
        (wrapped_len,) = struct.unpack_from(">I", blob, offset)
        offset += 4
        wrapped_key = blob[offset : offset + wrapped_len]
        offset += wrapped_len
        ciphertext = blob[offset:]
        if len(kem_ct) != kem_len or len(wrapped_key) != wrapped_len or not ciphertext:
            raise ValueError("truncated PQ envelope")
    except struct.error as e:
        raise ValueError(f"malformed PQ envelope header: {e}") from e

    shared_secret = ML_KEM_768.decaps(_b64d(settings.PQ_KEM_PRIVATE_KEY), kem_ct)
    try:
        file_key = _derive_wrap_cipher(shared_secret).decrypt(wrapped_key)
        return Fernet(file_key).decrypt(ciphertext)
    except InvalidToken as e:
        raise ValueError("PQ envelope failed authentication (tampered?)") from e


# ── ML-DSA token signatures (hybrid with the existing HMAC JWT) ─────────────

def sign_token(jwt_token: str) -> str:
    """Append an ML-DSA-44 signature to an already HMAC-signed JWT:
    '<jwt>~<b64url(signature)>'. CPU-bound; call via asyncio.to_thread on
    hot paths."""
    from dilithium_py.ml_dsa import ML_DSA_44

    if not settings.PQ_SIG_PRIVATE_KEY:
        raise ValueError("PQ_SIG_PRIVATE_KEY not configured")
    signature = ML_DSA_44.sign(_b64d(settings.PQ_SIG_PRIVATE_KEY), jwt_token.encode("utf-8"))
    return jwt_token + PQ_TOKEN_SEPARATOR + base64.urlsafe_b64encode(signature).decode("ascii")


def split_and_verify_token(token: str) -> Optional[str]:
    """Strip and verify the PQ layer of a token, returning the inner JWT.

    Contract (keeps every rollout state working):
      - Token has a PQ signature → it MUST verify against the configured
        public key, else None (treat as invalid). A PQ-signed token is never
        accepted on its signature's word alone — the caller still verifies
        the inner JWT's HMAC (hybrid: both layers must pass).
      - Token has no PQ signature → returned as-is. Legacy sessions issued
        before the flag was enabled stay valid for their natural TTL.
    """
    from dilithium_py.ml_dsa import ML_DSA_44

    if PQ_TOKEN_SEPARATOR not in token:
        return token

    jwt_part, _, sig_part = token.rpartition(PQ_TOKEN_SEPARATOR)
    if not jwt_part or not sig_part:
        return None
    if not settings.PQ_SIG_PUBLIC_KEY:
        # A PQ-signed token reached a deployment with no public key configured:
        # fail closed on the PQ layer rather than silently ignoring it.
        logger.warning("PQ-signed token received but PQ_SIG_PUBLIC_KEY is not set")
        return None
    try:
        signature = base64.urlsafe_b64decode(sig_part.encode("ascii"))
        if ML_DSA_44.verify(_b64d(settings.PQ_SIG_PUBLIC_KEY), jwt_part.encode("utf-8"), signature):
            return jwt_part
    except Exception:
        pass
    return None
