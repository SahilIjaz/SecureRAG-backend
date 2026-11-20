"""
Small-talk / greeting gate (app/services/small_talk_gate.py) and its wiring
into app/services/rag_service.py's _prepare_generation.

All pure / mocked — no network, no database.

Run with:  PYTHONPATH=. ./venv/bin/pytest tests/test_small_talk_gate.py -v
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services import small_talk_gate
from app.services.llm import router as llm_router


class _FakeUsage:
    provider = "gemini"
    model = "gemini-flash-lite-latest"


class _FakeResult:
    def __init__(self, text: str):
        self.text = text
        self.usage = _FakeUsage()


# ── Tier 1: free heuristic ───────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "hi", "Hi!", "hii", "hello", "Hello there", "hey", "heyyy", "yo",
    "good morning", "Good Morning!", "greetings",
    "thanks", "Thank you!", "thank you so much", "ty", "cheers",
    "ok", "okay", "great", "cool", "got it", "sounds good", "perfect",
    "bye", "goodbye", "see ya", "take care", "have a good day",
])
async def test_heuristic_matches_common_greetings_and_pleasantries(message):
    assert await small_talk_gate.is_small_talk(message) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "hi, do you ship to Canada?",
    "What is your refund policy?",
    "Can you tell me about pricing for your business plan",
    "hello I need help resetting my password",
    "thanks for the info, but does this cover international orders?",
])
async def test_real_questions_are_never_flagged_as_small_talk(message):
    # Each of these is either too long or contains "?", so the tier-2 LLM
    # check is never reached — confirms the short-circuit, not just the result.
    assert await small_talk_gate.is_small_talk(message) is False


@pytest.mark.asyncio
async def test_empty_message_is_not_small_talk():
    assert await small_talk_gate.is_small_talk("") is False
    assert await small_talk_gate.is_small_talk("   ") is False


# ── Gate disabled ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gate_disabled_always_returns_false(monkeypatch):
    monkeypatch.setattr(settings, "RAG_SMALL_TALK_GATE_ENABLED", False)
    assert await small_talk_gate.is_small_talk("hi") is False


# ── Tier 2: LLM confirmation for short, ambiguous messages ──────────────────

@pytest.mark.asyncio
async def test_tier2_confirms_ambiguous_short_message(monkeypatch):
    async def fake_classify(prompt, **kwargs):
        assert "how are you doing" in prompt
        return _FakeResult("YES")

    monkeypatch.setattr(llm_router, "classify", fake_classify)
    assert await small_talk_gate.is_small_talk("how are you doing") is True


@pytest.mark.asyncio
async def test_tier2_rejects_when_model_says_no(monkeypatch):
    async def fake_classify(prompt, **kwargs):
        return _FakeResult("NO")

    monkeypatch.setattr(llm_router, "classify", fake_classify)
    assert await small_talk_gate.is_small_talk("need help with my order") is False


@pytest.mark.asyncio
async def test_tier2_disabled_skips_llm_call_entirely(monkeypatch):
    calls = {"n": 0}

    async def fake_classify(prompt, **kwargs):
        calls["n"] += 1
        return _FakeResult("YES")

    monkeypatch.setattr(llm_router, "classify", fake_classify)
    monkeypatch.setattr(settings, "RAG_SMALL_TALK_LLM_CHECK", False)
    assert await small_talk_gate.is_small_talk("how are you doing") is False
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_tier2_failure_defaults_to_false(monkeypatch):
    async def fake_classify(prompt, **kwargs):
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr(llm_router, "classify", fake_classify)
    assert await small_talk_gate.is_small_talk("how are you doing") is False


@pytest.mark.asyncio
async def test_tier2_not_reached_for_questions_or_long_messages(monkeypatch):
    calls = {"n": 0}

    async def fake_classify(prompt, **kwargs):
        calls["n"] += 1
        return _FakeResult("YES")

    monkeypatch.setattr(llm_router, "classify", fake_classify)
    await small_talk_gate.is_small_talk("is this a question?")
    await small_talk_gate.is_small_talk("x" * 41)
    assert calls["n"] == 0


# ── Integration: _prepare_generation skips retrieval entirely ───────────────

@pytest.mark.asyncio
async def test_prepare_generation_skips_retrieval_for_small_talk(monkeypatch):
    from app.services import hybrid_search as hybrid_search_module
    from app.services import rag_service

    called = {"hybrid": False}

    async def fake_hybrid_search(*args, **kwargs):
        called["hybrid"] = True
        return []

    monkeypatch.setattr(hybrid_search_module, "hybrid_search", fake_hybrid_search)

    chunks, prompt, max_tokens, handoff, metrics = await rag_service._prepare_generation(
        "tenant-1", "hi there!", 1024, None, None, None,
    )
    assert chunks == []
    assert metrics["smallTalk"] is True
    assert called["hybrid"] is False
    assert "hi there!" in prompt


@pytest.mark.asyncio
async def test_prepare_generation_runs_retrieval_for_a_real_question(monkeypatch):
    from app.services import hybrid_search as hybrid_search_module
    from app.services import rag_service

    called = {"hybrid": False}

    async def fake_hybrid_search(*args, **kwargs):
        called["hybrid"] = True
        return []

    monkeypatch.setattr(hybrid_search_module, "hybrid_search", fake_hybrid_search)

    chunks, prompt, max_tokens, handoff, metrics = await rag_service._prepare_generation(
        "tenant-1", "What is your refund policy for defective items?", 1024, None, None, None,
    )
    assert called["hybrid"] is True
    assert not metrics.get("smallTalk")
