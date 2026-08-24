"""
Temporary structured request timing for the signup/onboarding latency audit
(Aug 2026). Records (label, elapsed_ms) segments per request via contextvars
so concurrent requests never cross-contaminate, then the middleware in
main.py flushes them as one log line per request.

Safe to strip once the culprits found here are fixed and re-measured.
"""

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import List, Optional, Tuple

_segments: ContextVar[Optional[List[Tuple[str, float]]]] = ContextVar("_segments", default=None)


def start() -> object:
    return _segments.set([])


def finish(token: object) -> List[Tuple[str, float]]:
    segments = _segments.get() or []
    _segments.reset(token)  # type: ignore[arg-type]
    return segments


@contextmanager
def timed(label: str):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        segments = _segments.get()
        if segments is not None:
            segments.append((label, round(elapsed_ms, 2)))


def mark(label: str, elapsed_ms: float) -> None:
    """Record a segment timed outside a `with timed()` block (e.g. a
    fire-and-forget background task that outlives the request)."""
    segments = _segments.get()
    if segments is not None:
        segments.append((label, round(elapsed_ms, 2)))
