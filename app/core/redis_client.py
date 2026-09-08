"""
Optional Redis connection.

Redis is a *best-effort* accelerator here, never a hard dependency: when
settings.REDIS_URL is set we use it (shared rate-limit state, a shared
query-embedding cache); when it's empty — or when Redis is unreachable at
runtime — callers fall back to the previous in-memory behaviour. This keeps
local dev zero-setup and means a Redis outage degrades performance rather than
taking the app down.

One async client is created at startup (init_redis) and closed at shutdown
(close_redis); get_redis() returns it, or None when Redis isn't configured.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

_redis = None  # type: ignore  # redis.asyncio.Redis | None, lazily typed to avoid a hard import


async def init_redis() -> None:
    """Connect to Redis if REDIS_URL is configured. Called from the app lifespan.
    A failure to connect is logged and swallowed — the app runs without Redis."""
    global _redis
    if not settings.REDIS_URL:
        logger.info("REDIS_URL not set — using in-memory rate limiting and embedding cache")
        return
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=3,
        )
        await client.ping()
        _redis = client
        logger.info("Connected to Redis")
    except Exception as e:
        _redis = None
        logger.warning("Could not connect to Redis (%s) — falling back to in-memory", e)


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        try:
            await _redis.aclose()
        except Exception:
            pass
        _redis = None


def get_redis():
    """The live async Redis client, or None when Redis isn't configured/available.
    Callers must handle None (in-memory fallback) and wrap calls in try/except —
    a mid-request Redis blip should never fail the request."""
    return _redis
