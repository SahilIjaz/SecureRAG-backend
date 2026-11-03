"""
Tests for the post-quantum crypto layer (app/core/pq_crypto.py) and its
integration points. Run with:

    PYTHONPATH=. ./venv/bin/pytest tests/ -v
"""

import base64

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.core import pq_crypto


@pytest.fixture(scope="module", autouse=True)
def pq_keys():
    """Fresh in-memory keypairs for the whole module; restores settings after."""
    saved = (
        settings.PQ_KEM_PUBLIC_KEY, settings.PQ_KEM_PRIVATE_KEY,
        settings.PQ_SIG_PUBLIC_KEY, settings.PQ_SIG_PRIVATE_KEY,
        settings.PQ_FILE_ENCRYPTION_ENABLED, settings.PQ_WIDGET_TOKEN_SIGNING_ENABLED,
    )
    settings.PQ_KEM_PUBLIC_KEY, settings.PQ_KEM_PRIVATE_KEY = pq_crypto.generate_kem_keypair()
    settings.PQ_SIG_PUBLIC_KEY, settings.PQ_SIG_PRIVATE_KEY = pq_crypto.generate_sig_keypair()
    settings.PQ_FILE_ENCRYPTION_ENABLED = True
    settings.PQ_WIDGET_TOKEN_SIGNING_ENABLED = True
    yield
    (
        settings.PQ_KEM_PUBLIC_KEY, settings.PQ_KEM_PRIVATE_KEY,
        settings.PQ_SIG_PUBLIC_KEY, settings.PQ_SIG_PRIVATE_KEY,
        settings.PQ_FILE_ENCRYPTION_ENABLED, settings.PQ_WIDGET_TOKEN_SIGNING_ENABLED,
    ) = saved


# ── Hybrid file encryption ───────────────────────────────────────────────────

def test_roundtrip_small_and_binary():
    for payload in [b"hello quantum", b"\x00\xff" * 500, b"x" * (1024 * 1024)]:
        blob = pq_crypto.hybrid_encrypt(payload)
        assert pq_crypto.is_pq_blob(blob)
        assert pq_crypto.hybrid_decrypt(blob) == payload


def test_each_file_gets_a_unique_envelope():
    a = pq_crypto.hybrid_encrypt(b"same content")
    b = pq_crypto.hybrid_decrypt(pq_crypto.hybrid_encrypt(b"same content"))
    assert b == b"same content"
    assert a != pq_crypto.hybrid_encrypt(b"same content")  # fresh key + fresh encaps


def test_legacy_fernet_blob_is_not_mistaken_for_pq():
    legacy = Fernet(Fernet.generate_key()).encrypt(b"old file")
    assert not pq_crypto.is_pq_blob(legacy)


def test_tampered_ciphertext_rejected():
    blob = bytearray(pq_crypto.hybrid_encrypt(b"integrity matters"))
    blob[-1] ^= 0xFF
    with pytest.raises(ValueError):
        pq_crypto.hybrid_decrypt(bytes(blob))


def test_tampered_wrapped_key_rejected():
    blob = bytearray(pq_crypto.hybrid_encrypt(b"integrity matters"))
    # Flip a byte inside the wrapped-key region (just past magic + 4 + kem_ct).
    blob[len(pq_crypto.PQ_MAGIC) + 4 + 1088 + 4 + 5] ^= 0xFF
    with pytest.raises(ValueError):
        pq_crypto.hybrid_decrypt(bytes(blob))


def test_truncated_blob_rejected():
    blob = pq_crypto.hybrid_encrypt(b"short")
    with pytest.raises(ValueError):
        pq_crypto.hybrid_decrypt(blob[: len(pq_crypto.PQ_MAGIC) + 3])


def test_wrong_private_key_fails_closed():
    blob = pq_crypto.hybrid_encrypt(b"secret")
    _, other_private = pq_crypto.generate_kem_keypair()
    saved = settings.PQ_KEM_PRIVATE_KEY
    settings.PQ_KEM_PRIVATE_KEY = other_private
    try:
        # ML-KEM implicit rejection yields a wrong shared secret → the wrap
        # layer's authentication must fail, never emit garbage plaintext.
        with pytest.raises(ValueError):
            pq_crypto.hybrid_decrypt(blob)
    finally:
        settings.PQ_KEM_PRIVATE_KEY = saved


# ── Widget token PQ signatures ───────────────────────────────────────────────

def test_token_sign_and_verify_roundtrip():
    from app.core.widget_auth import create_widget_session_token, decode_widget_session_token

    token = create_widget_session_token("tenant-1", "conv-1")
    assert pq_crypto.PQ_TOKEN_SEPARATOR in token  # PQ layer present
    payload = decode_widget_session_token(token)
    assert payload is not None
    assert payload["tenant_id"] == "tenant-1"
    assert payload["conversation_id"] == "conv-1"


def test_tampered_pq_signature_rejected():
    from app.core.widget_auth import create_widget_session_token, decode_widget_session_token

    token = create_widget_session_token("tenant-1", "conv-1")
    jwt_part, sep, sig_part = token.rpartition(pq_crypto.PQ_TOKEN_SEPARATOR)
    sig = bytearray(base64.urlsafe_b64decode(sig_part))
    sig[10] ^= 0xFF
    forged = jwt_part + sep + base64.urlsafe_b64encode(bytes(sig)).decode()
    assert decode_widget_session_token(forged) is None


def test_tampered_jwt_under_valid_looking_structure_rejected():
    from app.core.widget_auth import create_widget_session_token, decode_widget_session_token

    token = create_widget_session_token("tenant-1", "conv-1")
    jwt_part, sep, sig_part = token.rpartition(pq_crypto.PQ_TOKEN_SEPARATOR)
    # Change the JWT: the PQ signature over it must no longer verify.
    assert decode_widget_session_token(jwt_part[:-2] + "xx" + sep + sig_part) is None


def test_legacy_unsigned_token_still_accepted():
    from app.core.widget_auth import decode_widget_session_token

    saved = settings.PQ_WIDGET_TOKEN_SIGNING_ENABLED
    settings.PQ_WIDGET_TOKEN_SIGNING_ENABLED = False
    try:
        from app.core.widget_auth import create_widget_session_token

        legacy = create_widget_session_token("tenant-1", "conv-1")
        assert pq_crypto.PQ_TOKEN_SEPARATOR not in legacy
    finally:
        settings.PQ_WIDGET_TOKEN_SIGNING_ENABLED = saved
    # Verified fine even with PQ signing re-enabled (grace for live sessions).
    assert decode_widget_session_token(legacy) is not None


# ── Storage integration (decrypt_file dispatch) ──────────────────────────────

@pytest.mark.asyncio
async def test_decrypt_file_dispatches_on_blob_type():
    from app.core import storage

    saved = settings.FILE_ENCRYPTION_KEY
    settings.FILE_ENCRYPTION_KEY = Fernet.generate_key().decode()
    storage._cipher = None  # reset the cached cipher
    try:
        legacy_blob = Fernet(settings.FILE_ENCRYPTION_KEY.encode()).encrypt(b"legacy doc")
        assert await storage.decrypt_file(legacy_blob) == b"legacy doc"
        pq_blob = pq_crypto.hybrid_encrypt(b"quantum doc")
        assert await storage.decrypt_file(pq_blob) == b"quantum doc"
    finally:
        settings.FILE_ENCRYPTION_KEY = saved
        storage._cipher = None
