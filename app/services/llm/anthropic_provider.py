"""Anthropic (Claude) provider — re-enabled after being commented out as a
rollback path. The old code's `thinking={"type": "adaptive"}` parameter is
dropped, not fixed: it doesn't exist anywhere in the installed SDK's type
definitions (confirmed by inspection, not assumed), so every call using it
would have raised TypeError. Extended thinking also isn't needed for a
customer-support RAG reply — dropping it is strictly lower-risk than
guessing at a replacement parameter shape with no way to test against a
live key in this environment."""

from __future__ import annotations

import logging
from typing import AsyncIterator

from app.config import settings
from app.services.llm.base import GenerationUsage, LLMResult, LLMStreamChunk, ProviderTransientError

logger = logging.getLogger(__name__)

_anthropic_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import AsyncAnthropic

        _anthropic_client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _anthropic_client


def _usage(model: str, usage) -> GenerationUsage:
    input_tokens = getattr(usage, "input_tokens", None) if usage else None
    output_tokens = getattr(usage, "output_tokens", None) if usage else None
    total = (input_tokens or 0) + (output_tokens or 0) if (input_tokens is not None or output_tokens is not None) else None
    return GenerationUsage(
        provider="anthropic", model=model,
        prompt_tokens=input_tokens, completion_tokens=output_tokens, total_tokens=total,
    )


class AnthropicProvider:
    name = "anthropic"

    def model_chain(self) -> list[str]:
        # Single-model chain for now — Claude doesn't have this project's
        # "-latest alias got silently retired" problem Gemini does, so
        # there's no fallback-within-provider need yet.
        return [settings.ANTHROPIC_ANSWER_MODEL]

    async def generate(
        self, prompt: str, *, model: str, max_tokens: int, timeout_seconds: float
    ) -> LLMResult:
        import anthropic

        client = _get_anthropic_client()
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
                timeout=timeout_seconds,
            )
        except anthropic.APIError as e:
            raise ProviderTransientError(str(e)) from e

        text = next((block.text for block in response.content if block.type == "text"), "")
        return LLMResult(text=text, usage=_usage(model, response.usage))

    async def generate_stream(
        self, prompt: str, *, model: str, max_tokens: int, timeout_seconds: float
    ) -> AsyncIterator[LLMStreamChunk]:
        import anthropic

        client = _get_anthropic_client()
        try:
            async with client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
                timeout=timeout_seconds,
            ) as stream:
                async for text in stream.text_stream:
                    if text:
                        yield LLMStreamChunk(text=text)
                final_message = await stream.get_final_message()
                yield LLMStreamChunk(text="", usage=_usage(model, final_message.usage))
        except anthropic.APIError as e:
            raise ProviderTransientError(str(e)) from e

    async def classify(self, prompt: str, *, model: str, max_tokens: int) -> LLMResult:
        return await self.generate(prompt, model=model, max_tokens=max_tokens, timeout_seconds=settings.ANTHROPIC_REQUEST_TIMEOUT_SECONDS)

    async def health_check(self, model: str) -> bool:
        # This SDK version (0.25.1) has no models-list/metadata endpoint —
        # unlike Gemini there's no free reachability probe available, and a
        # real generation call would cost real money on every check. This
        # only confirms a key is configured, not that the key/model
        # combination actually works; a bad key still surfaces at the first
        # real call, same as it would have before this provider existed.
        return bool(settings.ANTHROPIC_API_KEY)
