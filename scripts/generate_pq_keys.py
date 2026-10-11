"""
One-time generator for the platform's post-quantum keypairs.

Prints the four .env lines for app/core/pq_crypto.py: an ML-KEM-768 keypair
(file-encryption envelopes) and an ML-DSA-44 keypair (widget-token
signatures). Run it once per environment and paste the output into that
environment's .env — the private keys are secrets of the same sensitivity as
SECRET_KEY / FILE_ENCRYPTION_KEY and must never be committed.

Usage:
    python scripts/generate_pq_keys.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.pq_crypto import generate_kem_keypair, generate_sig_keypair  # noqa: E402


def main() -> None:
    kem_public, kem_private = generate_kem_keypair()
    sig_public, sig_private = generate_sig_keypair()

    print("# Post-quantum keys — add to .env (keep the private keys secret!)")
    print(f"PQ_KEM_PUBLIC_KEY={kem_public}")
    print(f"PQ_KEM_PRIVATE_KEY={kem_private}")
    print(f"PQ_SIG_PUBLIC_KEY={sig_public}")
    print(f"PQ_SIG_PRIVATE_KEY={sig_private}")
    print("# Then enable the features:")
    print("PQ_FILE_ENCRYPTION_ENABLED=true")
    print("PQ_WIDGET_TOKEN_SIGNING_ENABLED=true")


if __name__ == "__main__":
    main()
