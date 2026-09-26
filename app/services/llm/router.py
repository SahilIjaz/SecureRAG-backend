"""Cross-provider router — generalizes rag_service.py's original per-model
retry loop to walk providers too. With the default single-provider config
(LLM_PROVIDER_CHAIN=gemini, no tenant preference), this reproduces the
original behavior exactly: one provider, one model-retry loop, no behavior
change from before this refactor.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from app.config import settings
from app.services.llm.anthropic_provider import AnthropicProvider
from app.services.llm.base import LLMProvider, LLMResult, LLMStreamChunk, ProviderTransientError
from app.services.llm.gemini_provider import GeminiProvider

logger = logging.getLogger(__name__)

# Registry of every provider this build knows how to construct — not the
# same as settings.LLM_PROVIDER_CHAIN_LIST (the platform's *default*
# fallback order). A tenant can select any provider in this registry even
# if it isn't in the default chain.
KNOWN_PROVIDERS = ("gemini", "anthropic")

_provider_instances: dict[str, LLMProvider] = {}


def _get_provider(name: str) -> LLMProvider | None:
    if name not in _provider_instances:
        if name == "gemini":
            _provider_instances[name] = GeminiProvider()
        elif name == "anthropic":
            _provider_instances[name] = AnthropicProvider()
        else:
            return None
    return _provider_instances[name]


def _resolve_provider_chain(tenant_provider: str | None) -> list[str]:
    """Tenant's chosen provider (if any) first, then the platform default
    chain as a safety net — a tenant who picked Claude still has Gemini to
    fall back on if Claude is down, without their choice silently and
    permanently reverting."""
    chain: list[str] = []
    if tenant_provider:
        chain.append(tenant_provider)
    for name in settings.LLM_PROVIDER_CHAIN_LIST:
        if name not in chain:
            chain.append(name)
    return chain


def _attempts(model_chain: list[str]) -> list[tuple[str, float]]:
    """Try each model once in order, then give the *last* one (the most
    deliberately chosen fallback) one more shot after a brief delay —
    same policy as the pre-refactor rag_service.py retry loop."""
    attempts = [(model, 0.0) for model in model_chain]
    if model_chain:
        attempts.append((model_chain[-1], 1.2))
    return attempts


async def generate(
    prompt: str, *, tenant_provider: str | None = None, max_tokens: int, timeout_seconds: float,
) -> LLMResult:
    last_error: Exception | None = None
    for provider_name in _resolve_provider_chain(tenant_provider):
        provider = _get_provider(provider_name)
        if provider is None:
            continue
        for model, delay in _attempts(provider.model_chain()):
            if delay:
                await asyncio.sleep(delay)
            try:
                return await provider.generate(
                    prompt, model=model, max_tokens=max_tokens, timeout_seconds=timeout_seconds
                )
            except ProviderTransientError as e:
                last_error = e
                logger.warning("%s model %s failed: %s", provider_name, model, e)
    if last_error:
        raise last_error
    raise RuntimeError("No LLM provider configured")


async def generate_stream(
    prompt: str, *, tenant_provider: str | None = None, max_tokens: int, timeout_seconds: float,
) -> AsyncIterator[LLMStreamChunk]:
    last_error: Exception | None = None
    for provider_name in _resolve_provider_chain(tenant_provider):
        provider = _get_provider(provider_name)
        if provider is None:
            continue
        for model, delay in _attempts(provider.model_chain()):
            if delay:
                await asyncio.sleep(delay)
            try:
                raw = provider.generate_stream(
                    prompt, model=model, max_tokens=max_tokens, timeout_seconds=timeout_seconds
                )
                iterator = raw.__aiter__()
                first_chunk = await iterator.__anext__()
            except ProviderTransientError as e:
                last_error = e
                logger.warning("%s model %s failed to start streaming: %s", provider_name, model, e)
                continue
            except StopAsyncIteration:
                last_error = RuntimeError(f"{provider_name}/{model} returned an empty stream")
                continue

            # Committed: once a model has yielded its first chunk, a
            # mid-stream failure becomes the caller's problem (an error
            # event), not a fallback to another model/provider — replaying
            # under a different model would duplicate or contradict text
            # already visible to the user.
            yield first_chunk
            async for chunk in iterator:
                yield chunk
            return
    if last_error:
        raise last_error
    raise RuntimeError("No LLM provider configured")


async def classify(prompt: str, *, provider_name: str, model: str, max_tokens: int) -> LLMResult:
    provider = _get_provider(provider_name)
    if provider is None:
        raise RuntimeError(f"Unknown provider {provider_name!r}")
    return await provider.classify(prompt, model=model, max_tokens=max_tokens)


async def health_check_all() -> dict[str, list[str]]:
    """{provider_name: [reachable models]} across every known provider
    (not just the ones in the default chain) — used by the startup
    reachability check and by GET /api/model-options to hide an
    unreachable option from the tenant-facing picker."""
    results: dict[str, list[str]] = {}
    for provider_name in KNOWN_PROVIDERS:
        provider = _get_provider(provider_name)
        if provider is None:
            continue
        reachable = []
        for model in provider.model_chain():
            try:
                if await provider.health_check(model):
                    reachable.append(model)
            except Exception as e:
                logger.warning("Health check for %s/%s crashed: %s", provider_name, model, e)
        results[provider_name] = reachable
    return results
