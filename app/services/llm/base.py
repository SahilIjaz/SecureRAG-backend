"""Provider-agnostic LLM interface.

Every provider (gemini_provider.py, anthropic_provider.py, and any future
one — Ollama included, whenever it's added) implements LLMProvider against
this shape so router.py, pricing.py and the wallet-billing call sites never
need to know which SDK actually answered. A prompt goes in, plain text comes
out — persona/behavior/RAG prompt-building stays in rag_service.py, never
crosses this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol, runtime_checkable


class ProviderTransientError(Exception):
    """Raised by a provider for a failure worth retrying (rate limit, timeout,
    overload) — router.py catches exactly this type to move to the next
    model/provider. Anything else propagates as a genuine bug, not a
    fallback trigger."""


@dataclass
class GenerationUsage:
    """Token accounting for one completed call. Mandatory on every
    successful LLMResult / terminal LLMStreamChunk — the wallet-billing
    call path depends on this being present to compute cost."""

    provider: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


@dataclass
class LLMResult:
    text: str
    usage: GenerationUsage


@dataclass
class LLMStreamChunk:
    """One increment of a streamed reply. `usage` is populated only on the
    terminal chunk (providers report cumulative/final usage differently —
    see each provider's generate_stream docstring for its own shape)."""

    text: str
    usage: GenerationUsage | None = None


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def model_chain(self) -> list[str]:
        """This provider's own models to try, primary first. The router
        walks this list (plus one extra delayed retry of the last entry)
        before moving to the next provider in the platform/tenant chain."""
        ...

    async def generate(
        self, prompt: str, *, model: str, max_tokens: int, timeout_seconds: float
    ) -> LLMResult: ...

    def generate_stream(
        self, prompt: str, *, model: str, max_tokens: int, timeout_seconds: float
    ) -> AsyncIterator[LLMStreamChunk]: ...

    async def classify(self, prompt: str, *, model: str, max_tokens: int) -> LLMResult: ...

    async def health_check(self, model: str) -> bool:
        """Cheap reachability probe (metadata fetch, never a real
        generation) — used both by the startup sanity check and by
        GET /api/model-options to hide an unreachable provider from the
        tenant-facing picker."""
        ...
