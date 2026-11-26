"""Shared pytest setup for the Nexus backend test-suite.

* Sets the minimum environment the app needs *before* ``app`` is imported.
* If the tiktoken encoding cannot be downloaded (offline CI / sandbox), a
  deterministic whitespace tokenizer is substituted so chunking tests still
  run. On a normal machine the real gpt-3.5-turbo encoding is used.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("SECRET_KEY", "unit-test-secret-key-not-for-production-0123456789")
os.environ.setdefault("DEBUG", "false")

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import tiktoken  # noqa: E402


class _FallbackEncoding:
    """Whitespace tokenizer with the encode/decode surface chunking uses."""

    def encode(self, text, **_kw):
        return [w for w in text.replace("\n", " \n ").split(" ") if w != ""]

    def decode(self, tokens):
        return " ".join(tokens)


try:
    tiktoken.encoding_for_model("gpt-3.5-turbo")
    USING_REAL_TOKENIZER = True
except Exception:  # network blocked
    USING_REAL_TOKENIZER = False
    tiktoken.encoding_for_model = lambda *_a, **_k: _FallbackEncoding()
