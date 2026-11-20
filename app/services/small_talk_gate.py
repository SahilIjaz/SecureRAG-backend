"""
Small-talk / greeting gate.

A plain greeting or pleasantry ("hi", "thanks", "bye") needs no knowledge-base
lookup, but without this gate it still runs the full retrieval pipeline —
embedding the query, searching Pinecone, Postgres hybrid search, reranking,
and scanning for conflicts — before the model ever sees it. This module
decides, cheaply, whether a message can skip all of that.

Two tiers, cheapest first:
  1. A free, deliberately narrow regex pass that matches ONLY unambiguous
     greetings/pleasantries — the whole (stripped) message, not a prefix, so
     "hi, do you ship to Canada?" does NOT match just because it starts
     with "hi".
  2. For anything short enough to plausibly be small talk but not caught by
     the regex, one cheap classification call (the same lite model already
     used for sentiment tagging) confirms it before skipping retrieval.

Conservative by construction: anything not confidently caught by either tier
falls through to the normal retrieval pipeline. A missed real question is far
worse than an occasional wasted retrieval on a borderline greeting.
"""

import logging
import re

from app.config import settings
from app.services.llm import router as llm_router

logger = logging.getLogger(__name__)

_GREETING_RE = re.compile(
    r"^(h+i+|h+e+l+o+|h+e+y+|yo+|sup|good\s?(morning|afternoon|evening|day)|greetings)[\s!.,]*$",
    re.IGNORECASE,
)
_PLEASANTRY_RE = re.compile(
    r"^("
    r"thanks?( you)?( so much)?( a lot)?|ty|cheers|much appreciated|appreciate it|"
    r"ok(ay)?|great|cool|got it|sounds good|perfect|awesome|nice one|"
    r"bye|goodbye|see ya|see you|take care|have a (good|nice|great) (day|one)"
    r")[\s!.,]*$",
    re.IGNORECASE,
)

# Long enough to cover "how are you doing today?" (a pleasantry, not a real
# question), short enough that a genuine question has no room to hide.
_MAX_TIER2_LEN = 40

def _matches_heuristic(text: str) -> bool:
    return bool(_GREETING_RE.match(text) or _PLEASANTRY_RE.match(text))

async def is_small_talk(message: str) -> bool:
    """
    True if `message` needs no knowledge-base lookup — a greeting or a short
    pleasantry, nothing that reads as an actual question or request.

    Conservative: any failure, timeout, ambiguity, or disabled config
    defaults to False (run the real retrieval pipeline).
    """
    if not settings.RAG_SMALL_TALK_GATE_ENABLED:
        return False

    text = (message or "").strip()
    if not text:
        return False
    if _matches_heuristic(text):
        return True

    # A question mark, or anything too long, is never small talk — don't
    # even spend the classification call.
    if not settings.RAG_SMALL_TALK_LLM_CHECK or len(text) > _MAX_TIER2_LEN or "?" in text:
        return False

    prompt = (
        "A customer support chatbot received this message from a user: "
        f"{text[:200]!r}\n"
        "Is this ONLY a greeting or small talk (e.g. hello, thanks, bye, how "
        "are you) with no actual question or request in it?\n"
        "Reply with exactly one word: YES or NO."
    )
    try:
        result = await llm_router.classify(
            prompt, provider_name="gemini", model=settings.GEMINI_CLASSIFICATION_MODEL,
            max_tokens=5,
        )
        return (result.text or "").strip().upper().startswith("YES")
    except Exception as e:
        logger.info("Small-talk classification failed, defaulting to full retrieval: %s", e)
        return False
