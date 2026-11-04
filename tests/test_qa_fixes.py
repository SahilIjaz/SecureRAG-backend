"""
Regression tests for the 2026-11-03 production-readiness pass (see
BUGS.md at the workspace root). Each test names the bug it would have caught.

Run with:  PYTHONPATH=. ./venv/bin/pytest tests/ -v
"""

import re
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import settings
from app.core import widget_auth
from app.core.entitlements import PLAN_ENTITLEMENTS, apply_plan_restrictions
from app.models.subscription import PlanName
from app.schemas import frontend as fe
from app.services.llm import router as llm_router


# ── B-003 / B-004 / B-013 / B-014 / B-020: server-side validation ────────────

@pytest.mark.parametrize("bad", ["weakweak", "alllowercase1!", "ALLUPPER1!", "NoDigits!!", "NoSpecial1a", "Sh0r!t"])
def test_weak_passwords_rejected_everywhere(bad):
    for model, payload in (
        (fe.FESignupRequest, {"email": "a@b.com", "companyName": "Acme"}),
        (fe.FEResetPasswordRequest, {"email": "a@b.com", "otp": "123456"}),
        (fe.FEChangePasswordRequest, {"currentPassword": "x"}),
        (fe.FEAcceptInviteRequest, {"token": "t", "name": "Agent"}),
    ):
        field = "newPassword" if "newPassword" in model.model_fields else "password"
        with pytest.raises(ValidationError):
            model(**payload, **{field: bad})


def test_strong_password_accepted_everywhere():
    good = "QaPassw0rd!"
    assert fe.FESignupRequest(email="a@b.com", companyName="Acme", password=good).password == good
    assert fe.FEResetPasswordRequest(email="a@b.com", otp="123456", newPassword=good).newPassword == good
    assert fe.FEChangePasswordRequest(currentPassword="x", newPassword=good).newPassword == good
    assert fe.FEAcceptInviteRequest(token="t", name="Agent", password=good).password == good


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_whitespace_only_names_rejected(blank):
    with pytest.raises(ValidationError):
        fe.FESignupRequest(email="a@b.com", companyName=blank, password="QaPassw0rd!")
    with pytest.raises(ValidationError):
        fe.FESendReplyRequest(text=blank)
    with pytest.raises(ValidationError):
        fe.FESaveProfileRequest(name=blank, email="a@b.com", companyName="Acme")
    with pytest.raises(ValidationError):
        fe.FESaveProfileRequest(name="Me", email="a@b.com", companyName=blank)


def test_names_are_stripped():
    assert fe.FESignupRequest(email="a@b.com", companyName="  Acme  ", password="QaPassw0rd!").companyName == "Acme"
    assert fe.FESendReplyRequest(text="  hi  ").text == "hi"


@pytest.mark.parametrize("color", ["#D97706", "#fff", "#aabbccdd", "rgb(10, 20, 30)", "hsla(10,20%,30%,0.5)", "tomato"])
def test_safe_accent_colors_accepted(color):
    assert fe.validate_css_color(color) == color


@pytest.mark.parametrize("color", ["javascript:alert(1)", "url(http://x)", "#fff; background:url(x)", "red}body{", "<script>"])
def test_unsafe_accent_colors_rejected(color):
    with pytest.raises(ValueError):
        fe.validate_css_color(color)


# ── B-002: plan fallback must be a frontend plan id ───────────────────────────

def test_fe_plan_fallback_is_a_valid_frontend_id():
    from app.api.frontend import helpers

    assert helpers.fe_plan_for_subscription(None) == "starter"
    assert helpers.fe_plan_for_subscription(None) in helpers.PLAN_DISPLAY_LIMITS
    assert helpers.fe_plan_for_subscription(None) in helpers.PLAN_PRICE_LABELS


# ── B-001: onboarding completion comes from the tenant, not the subscription ──

def test_onboarding_completed_requires_completed_at():
    from app.api.frontend import helpers

    assert helpers.onboarding_completed(SimpleNamespace(onboarding_completed_at=None)) is False
    assert helpers.onboarding_completed(SimpleNamespace(onboarding_completed_at=datetime.now(timezone.utc))) is True


# ── B-019: paid chatbot features stripped for plans that lack them ────────────

def _full_config():
    return {
        "behavior": {"collectEmailBeforeChat": True, "collectNameBeforeChat": True, "collectPhoneBeforeChat": True, "handoffToHuman": True},
        "appearance": {"showPoweredBy": False},
        "menu": [{"label": "Drinks", "items": []}],
    }


def test_starter_plan_loses_paid_features():
    cfg = apply_plan_restrictions(_full_config(), PLAN_ENTITLEMENTS[PlanName.free])
    assert cfg["menu"] == []
    assert cfg["behavior"]["collectEmailBeforeChat"] is False
    assert cfg["behavior"]["collectNameBeforeChat"] is False
    assert cfg["behavior"]["collectPhoneBeforeChat"] is False
    assert cfg["behavior"]["handoffToHuman"] is False
    assert cfg["appearance"]["showPoweredBy"] is True


def test_growth_plan_keeps_lead_capture_but_not_branding_removal():
    cfg = apply_plan_restrictions(_full_config(), PLAN_ENTITLEMENTS[PlanName.pro])
    assert cfg["menu"] != []
    assert cfg["behavior"]["collectEmailBeforeChat"] is True
    assert cfg["behavior"]["handoffToHuman"] is True
    assert cfg["appearance"]["showPoweredBy"] is True


def test_business_plan_keeps_everything():
    cfg = apply_plan_restrictions(_full_config(), PLAN_ENTITLEMENTS[PlanName.pro_plus])
    assert cfg["appearance"]["showPoweredBy"] is False
    assert cfg["menu"] != []


# ── B-024: the embedding site is read from X-Widget-Host when the call comes
#          from our own widget iframe, and from Origin otherwise ─────────────

FRONTEND = settings.FRONTEND_URL  # e.g. http://localhost:5173


def test_iframe_call_uses_widget_host_header():
    assert widget_auth.resolve_embed_host(FRONTEND, "acme.com") == "acme.com"
    assert widget_auth.resolve_embed_host(FRONTEND, "https://www.acme.com/page") == "www.acme.com"


def test_iframe_call_without_widget_host_is_rejected():
    assert widget_auth.resolve_embed_host(FRONTEND, None) is None
    assert widget_auth.is_embed_allowed(FRONTEND, None, "acme.com") is False


def test_customer_following_the_docs_is_now_allowed():
    # Deploy tab says "Allowed domains = acme.com"; the iframe reports acme.com.
    assert widget_auth.is_embed_allowed(FRONTEND, "acme.com", "acme.com, www.acme.com") is True


def test_embedding_site_must_match_exactly():
    assert widget_auth.is_embed_allowed(FRONTEND, "evil-acme.com", "acme.com") is False
    assert widget_auth.is_embed_allowed(FRONTEND, "acme.com.evil.net", "acme.com") is False
    assert widget_auth.is_embed_allowed(FRONTEND, "sub.acme.com", "acme.com") is False


def test_direct_browser_call_checks_origin_and_ignores_header():
    # A page on evil.example can't borrow an allowed host via the header.
    assert widget_auth.is_embed_allowed("https://evil.example", "acme.com", "acme.com") is False
    assert widget_auth.is_embed_allowed("https://acme.com", None, "acme.com") is True


def test_origin_less_caller_follows_require_origin_flag(monkeypatch):
    monkeypatch.setattr(settings, "WIDGET_REQUIRE_ORIGIN", True)
    assert widget_auth.is_embed_allowed(None, "acme.com", "acme.com") is False
    monkeypatch.setattr(settings, "WIDGET_REQUIRE_ORIGIN", False)
    assert widget_auth.is_embed_allowed(None, None, "acme.com") is True


# ── B-017: end-to-end LLM budget ──────────────────────────────────────────────

def test_budget_clamps_and_skips_attempts():
    b = llm_router._Budget(total_seconds=30.0)
    assert b.remaining_for(20.0) == pytest.approx(20.0, abs=0.5)
    b.started -= 27.0  # pretend 27s have passed
    assert b.remaining_for(20.0) is None  # <5s left: don't start another model
    b.started += 10.0  # 17s passed -> 13s left
    assert b.remaining_for(20.0) == pytest.approx(13.0, abs=0.5)


@pytest.mark.asyncio
async def test_generate_stops_at_budget(monkeypatch):
    calls = []

    class SlowProvider:
        def model_chain(self):
            return ["m1", "m2"]

        async def generate(self, prompt, *, model, max_tokens, timeout_seconds):
            calls.append((model, timeout_seconds))
            raise llm_router.ProviderTransientError(f"{model} timed out")

    monkeypatch.setattr(llm_router, "_get_provider", lambda name: SlowProvider() if name == "gemini" else None)
    monkeypatch.setattr(settings, "LLM_PROVIDER_CHAIN", "gemini")
    monkeypatch.setattr(settings, "LLM_TOTAL_BUDGET_SECONDS", 0.0)  # nothing fits
    with pytest.raises(llm_router.ProviderTransientError) as exc:
        await llm_router.generate("hi", max_tokens=10, timeout_seconds=20.0)
    assert "No answer within" in str(exc.value)
    assert calls == []  # no attempt even started


# ── B-023: failure reasons are user-readable ─────────────────────────────────

def test_failure_reason_maps_pdf_errors():
    from app.services.indexing_service import _failure_reason

    class PdfReadError(Exception):
        pass

    assert "corrupt" in _failure_reason(PdfReadError("startxref not found"))
    assert "Timed out" in _failure_reason(TimeoutError("read timed out"))
    assert len(_failure_reason(RuntimeError("x" * 2000))) <= 500
