"""Gemini provider — lifted from the pre-refactor rag_service.py, unchanged
in behavior. Single-model, single-attempt calls only; router.py owns the
retry/fallback loop across settings.GEMINI_MODEL_CHAIN."""

from __future__ import annotations

import logging
from typing import AsyncIterator

from app.config import settings
from app.services.llm.base import GenerationUsage, LLMResult, LLMStreamChunk, ProviderTransientError

logger = logging.getLogger(__name__)

_genai_client = None


def _get_genai_client():
    """Lazy-cached Gemini client — was previously reconstructed on every chat request."""
    global _genai_client
    if _genai_client is None:
        from google import genai

        _genai_client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _genai_client


def _usage_from_response(model: str, response) -> GenerationUsage:
    meta = getattr(response, "usage_metadata", None)
    return GenerationUsage(
        provider="gemini",
        model=model,
        prompt_tokens=getattr(meta, "prompt_token_count", None) if meta else None,
        completion_tokens=getattr(meta, "candidates_token_count", None) if meta else None,
        total_tokens=getattr(meta, "total_token_count", None) if meta else None,
    )


class GeminiProvider:
    name = "gemini"

    def model_chain(self) -> list[str]:
        return settings.GEMINI_MODEL_CHAIN

    async def generate(
        self, prompt: str, *, model: str, max_tokens: int, timeout_seconds: float
    ) -> LLMResult:
        import httpx
        from google.genai import errors as genai_errors
        from google.genai import types as genai_types

        client = _get_genai_client()
        http_options = genai_types.HttpOptions(timeout=int(timeout_seconds * 1000))
        try:
            response = await client.aio.models.generate_content(
                model=model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=max_tokens,
                    http_options=http_options,
                ),
            )
        except genai_errors.APIError as e:
            raise ProviderTransientError(str(e)) from e
        except httpx.TimeoutException as e:
            raise ProviderTransientError(f"Gemini model {model} timed out after {timeout_seconds:.0f}s") from e

        return LLMResult(text=response.text or "", usage=_usage_from_response(model, response))

    async def generate_stream(
        self, prompt: str, *, model: str, max_tokens: int, timeout_seconds: float
    ) -> AsyncIterator[LLMStreamChunk]:
        import httpx
        from google.genai import errors as genai_errors
        from google.genai import types as genai_types

        client = _get_genai_client()
        http_options = genai_types.HttpOptions(timeout=int(timeout_seconds * 1000))
        try:
            raw = await client.aio.models.generate_content_stream(
                model=model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=max_tokens,
                    http_options=http_options,
                ),
            )
        except genai_errors.APIError as e:
            raise ProviderTransientError(str(e)) from e
        except httpx.TimeoutException as e:
            raise ProviderTransientError(f"Gemini model {model} timed out starting stream after {timeout_seconds:.0f}s") from e

        last_usage: GenerationUsage | None = None
        try:
            async for chunk in raw:
                text = getattr(chunk, "text", None) or ""
                meta = getattr(chunk, "usage_metadata", None)
                if meta is not None:
                    # Gemini reports cumulative usage on every chunk that
                    # carries it — keep only the latest, never sum these.
                    last_usage = _usage_from_response(model, chunk)
                if text:
                    yield LLMStreamChunk(text=text)
        except genai_errors.APIError as e:
            raise ProviderTransientError(str(e)) from e
        except httpx.TimeoutException as e:
            raise ProviderTransientError(f"Gemini model {model} timed out mid-stream") from e

        # Terminal usage-only chunk, empty text — callers already skip
        # empty-text chunks, so this can't be mistaken for visible output.
        yield LLMStreamChunk(text="", usage=last_usage)

    async def classify(self, prompt: str, *, model: str, max_tokens: int) -> LLMResult:
        from google.genai import errors as genai_errors
        from google.genai import types as genai_types

        client = _get_genai_client()
        try:
            response = await client.aio.models.generate_content(
                model=model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(max_output_tokens=max_tokens),
            )
        except genai_errors.APIError as e:
            raise ProviderTransientError(str(e)) from e

        return LLMResult(text=response.text or "", usage=_usage_from_response(model, response))

    async def health_check(self, model: str) -> bool:
        from google.genai import errors as genai_errors

        if not settings.GEMINI_API_KEY:
            return False
        client = _get_genai_client()
        try:
            await client.aio.models.get(model=model)
            return True
        except genai_errors.APIError as e:
            logger.warning(
                "Gemini model %r is NOT reachable with the configured API key (%s). "
                "Dated/pinned model names are the most likely to be retired — prefer "
                "'-latest' aliases.",
                model, e,
            )
            return False
        except Exception as e:
            logger.warning("Could not verify Gemini model %r (%s): %s", model, type(e).__name__, e)
            return False
