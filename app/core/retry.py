"""Small retry-with-backoff helper for flaky third-party calls (Pinecone).

Hand-rolled rather than a new dependency (tenacity/backoff) — consistent with
this project's preference for stdlib-only fixes where the need is this small
(see app/core/validation.py's magic-byte sniffing instead of python-magic).
"""

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

async def retry_async(
    fn: Callable[..., Awaitable[T]],
    *args,
    retries: int = 3,
    base_delay: float = 1.0,
    **kwargs,
) -> T:
    """Call `fn(*args, **kwargs)`, retrying on any exception up to `retries`
    times with exponential backoff (base_delay, base_delay*2, base_delay*4, ...).
    Re-raises the last exception if every attempt fails."""
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            last_error = e
            if attempt == retries:
                break
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                getattr(fn, "__name__", fn), attempt, retries, e, delay,
            )
            await asyncio.sleep(delay)
    raise last_error
