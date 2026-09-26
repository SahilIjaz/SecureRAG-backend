"""
Text extraction from images and scanned PDFs via Gemini vision.

Used by the scraper (text inside pictures on a web page — menus, flyers,
price lists, infographics) and by the indexer (PDFs that have no text
layer, i.e. scans). The knowledge base is text-only, so without this any
information that exists only as pixels is invisible to retrieval.

Every call returns the model's GenerationUsage alongside the text so the
caller can bill it through wallet_service.record_usage — this module never
touches the wallet itself and never decides whether OCR is *allowed*
(the caller gates on settings.SCRAPE_OCR_ENABLED + the tenant's trial /
wallet state before calling in).
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from app.config import settings
from app.services.llm.base import GenerationUsage, ProviderTransientError

logger = logging.getLogger(__name__)

NO_TEXT = "NO_TEXT"

_IMAGE_PROMPT = (
    "Extract every piece of readable text from this image, exactly as written, keeping the "
    "natural reading order (top to bottom, left to right; tables row by row, one row per line). "
    "Include headings, labels, prices, numbers, dates and small print. Output plain text only: "
    "no markdown, no description of the picture, no commentary. "
    f"If the image contains no readable text at all, reply with exactly: {NO_TEXT}"
)

_PDF_PROMPT = (
    "This PDF is a scanned document with no text layer. Transcribe all readable text page by "
    "page, in order, exactly as written. Start each page with a line 'Page N'. Output plain text "
    "only: no markdown, no commentary. "
    f"If there is no readable text at all, reply with exactly: {NO_TEXT}"
)

# Gemini's inline-data ceiling is 20MB per request; stay under it.
MAX_INLINE_BYTES = 18 * 1024 * 1024


async def extract_text_from_image(data: bytes, mime_type: str) -> Tuple[str, Optional[GenerationUsage]]:
    """Text in an image, or '' when the model reports none. Raises
    ProviderTransientError on a Gemini failure so callers can skip the
    image rather than fail the whole page."""
    return await _generate(data, mime_type, _IMAGE_PROMPT)


async def extract_text_from_pdf(data: bytes) -> Tuple[str, Optional[GenerationUsage]]:
    """Transcribe a scanned PDF (no text layer). Same contract as
    extract_text_from_image."""
    if len(data) > MAX_INLINE_BYTES:
        raise ProviderTransientError(
            f"PDF is {len(data) / 1024 / 1024:.1f}MB, above the {MAX_INLINE_BYTES // 1024 // 1024}MB OCR limit"
        )
    return await _generate(data, "application/pdf", _PDF_PROMPT)


async def _generate(data: bytes, mime_type: str, prompt: str) -> Tuple[str, Optional[GenerationUsage]]:
    import httpx
    from google.genai import errors as genai_errors
    from google.genai import types as genai_types

    from app.services.llm.gemini_provider import _get_genai_client, _usage_from_response

    if not settings.GEMINI_API_KEY:
        raise ProviderTransientError("GEMINI_API_KEY is not configured")

    model = settings.GEMINI_OCR_MODEL
    client = _get_genai_client()
    try:
        response = await client.aio.models.generate_content(
            model=model,
            contents=[genai_types.Part.from_bytes(data=data, mime_type=mime_type), prompt],
            config=genai_types.GenerateContentConfig(
                max_output_tokens=settings.SCRAPE_OCR_MAX_OUTPUT_TOKENS,
                temperature=0.0,
                http_options=genai_types.HttpOptions(timeout=int(settings.SCRAPE_OCR_TIMEOUT_SECONDS * 1000)),
            ),
        )
    except genai_errors.APIError as e:
        raise ProviderTransientError(str(e)) from e
    except httpx.TimeoutException as e:
        raise ProviderTransientError(f"OCR timed out after {settings.SCRAPE_OCR_TIMEOUT_SECONDS:.0f}s") from e

    usage = _usage_from_response(model, response)
    text = (response.text or "").strip()
    if not text or text.upper().startswith(NO_TEXT):
        return "", usage
    return text, usage
