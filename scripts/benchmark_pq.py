"""
Benchmark the post-quantum layer against the classical baseline.

Measures, on this machine:
  - primitive costs: ML-KEM-768 keygen/encaps/decaps, ML-DSA-44 keygen/sign/
    verify, and the classical HMAC-JWT encode/decode for comparison
  - artifact sizes: keys, KEM ciphertext, signatures, token growth
  - end-to-end file encryption: legacy Fernet vs hybrid PQ envelope at
    1 KB / 1 MB / 10 MB

Run (keys are generated in-memory; no .env needed):
    PYTHONPATH=. ./venv/bin/python scripts/benchmark_pq.py
"""

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.fernet import Fernet  # noqa: E402
from jose import jwt  # noqa: E402

from app.config import settings  # noqa: E402
from app.core import pq_crypto  # noqa: E402


def timeit(fn, n=10):
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)
    return statistics.median(samples)


def main() -> None:
    from kyber_py.ml_kem import ML_KEM_768
    from dilithium_py.ml_dsa import ML_DSA_44

    # In-memory keys so the benchmark is self-contained.
    kem_pub, kem_priv = pq_crypto.generate_kem_keypair()
    sig_pub, sig_priv = pq_crypto.generate_sig_keypair()
    settings.PQ_KEM_PUBLIC_KEY = kem_pub
    settings.PQ_KEM_PRIVATE_KEY = kem_priv
    settings.PQ_SIG_PUBLIC_KEY = sig_pub
    settings.PQ_SIG_PRIVATE_KEY = sig_priv

    print("## Primitive operations (median ms)\n")
    print("| Operation | Median (ms) |")
    print("|---|---|")

    ek, dk = ML_KEM_768.keygen()
    ss, kem_ct = ML_KEM_768.encaps(ek)
    pk, sk = ML_DSA_44.keygen()
    msg = b"x" * 300
    sig = ML_DSA_44.sign(sk, msg)

    rows = [
        ("ML-KEM-768 keygen", lambda: ML_KEM_768.keygen()),
        ("ML-KEM-768 encaps", lambda: ML_KEM_768.encaps(ek)),
        ("ML-KEM-768 decaps", lambda: ML_KEM_768.decaps(dk, kem_ct)),
        ("ML-DSA-44 keygen", lambda: ML_DSA_44.keygen()),
        ("ML-DSA-44 sign (300B msg)", lambda: ML_DSA_44.sign(sk, msg)),
        ("ML-DSA-44 verify", lambda: ML_DSA_44.verify(pk, msg, sig)),
        ("HS256 JWT encode (classical)", lambda: jwt.encode({"a": 1}, "k", algorithm="HS256")),
        ("HS256 JWT decode (classical)", lambda: jwt.decode(jwt.encode({"a": 1}, "k", algorithm="HS256"), "k", algorithms=["HS256"])),
    ]
    for name, fn in rows:
        print(f"| {name} | {timeit(fn):.1f} |")

    print("\n## Artifact sizes (bytes)\n")
    print("| Artifact | Size |")
    print("|---|---|")
    jwt_token = jwt.encode({"tenant_id": "t", "conversation_id": "c"}, "k", algorithm="HS256")
    pq_token = pq_crypto.sign_token(jwt_token)
    for name, size in [
        ("ML-KEM-768 public key", len(ek)),
        ("ML-KEM-768 KEM ciphertext (per file)", len(kem_ct)),
        ("ML-DSA-44 public key", len(pk)),
        ("ML-DSA-44 signature", len(sig)),
        ("Widget token, classical JWT", len(jwt_token)),
        ("Widget token, PQ-signed", len(pq_token)),
    ]:
        print(f"| {name} | {size} |")

    print("\n## File encryption end-to-end (median ms)\n")
    print("| Payload | Fernet encrypt | PQ hybrid encrypt | Fernet decrypt | PQ hybrid decrypt | PQ size overhead |")
    print("|---|---|---|---|---|---|")
    legacy = Fernet(Fernet.generate_key())
    for label, size in [("1 KB", 1024), ("1 MB", 1024**2), ("10 MB", 10 * 1024**2)]:
        payload = b"a" * size
        legacy_ct = legacy.encrypt(payload)
        pq_ct = pq_crypto.hybrid_encrypt(payload)
        n = 5 if size <= 1024**2 else 3
        print(
            f"| {label} "
            f"| {timeit(lambda: legacy.encrypt(payload), n):.1f} "
            f"| {timeit(lambda: pq_crypto.hybrid_encrypt(payload), n):.1f} "
            f"| {timeit(lambda: legacy.decrypt(legacy_ct), n):.1f} "
            f"| {timeit(lambda: pq_crypto.hybrid_decrypt(pq_ct), n):.1f} "
            f"| +{len(pq_ct) - len(legacy_ct)} B |"
        )


if __name__ == "__main__":
    main()
