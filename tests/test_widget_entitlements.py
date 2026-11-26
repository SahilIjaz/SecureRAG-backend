"""Unit tests: widget origin allow-list + session token, and plan entitlements."""
import pytest

from app.config import settings
from app.core import widget_auth as wa
from app.core.entitlements import (
    PLAN_ENTITLEMENTS, apply_plan_restrictions, entitlements_for_plan, get_entitlements)
from app.models.subscription import PlanName


# ── origin allow-list ───────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _require_origin(monkeypatch):
    monkeypatch.setattr(settings, "WIDGET_REQUIRE_ORIGIN", True)


def test_exact_host_allowed():
    assert wa.is_origin_allowed("https://acme.com", "acme.com")


def test_allowlist_entry_forms_normalise():
    for entry in ("acme.com", "https://acme.com", "acme.com:8080", "ACME.com/path"):
        assert wa.is_origin_allowed("https://acme.com", entry)


def test_suffix_and_prefix_attacks_blocked():
    assert not wa.is_origin_allowed("https://evil-acme.com", "acme.com")
    assert not wa.is_origin_allowed("https://acme.com.evil.net", "acme.com")


def test_missing_origin_blocked_by_default_and_allowed_by_flag(monkeypatch):
    assert not wa.is_origin_allowed(None, "acme.com")
    monkeypatch.setattr(settings, "WIDGET_REQUIRE_ORIGIN", False)
    assert wa.is_origin_allowed(None, "acme.com")


def test_multiple_domains_csv():
    assert wa.is_origin_allowed("https://b.io", "a.io, b.io ,c.io")


def test_empty_allowlist_blocks_everything():
    assert not wa.is_origin_allowed("https://acme.com", "")


# ── session token ───────────────────────────────────────────────────────────
def test_widget_token_roundtrip():
    p = wa.decode_widget_session_token(wa.create_widget_session_token("t1", "c1"))
    assert p["tenant_id"] == "t1" and p["conversation_id"] == "c1"


@pytest.mark.parametrize("bad", ["", "junk", "a.b.c"])
def test_widget_token_garbage_returns_none(bad):
    assert wa.decode_widget_session_token(bad) is None


def test_dashboard_jwt_is_not_a_widget_session():
    from app.core.security import create_access_token
    assert wa.decode_widget_session_token(create_access_token({"sub": "u"})) is None


# ── entitlements ────────────────────────────────────────────────────────────
def test_plan_limits_increase_with_tier():
    f, p, b = (PLAN_ENTITLEMENTS[x] for x in (PlanName.free, PlanName.pro, PlanName.pro_plus))
    assert f.max_documents < p.max_documents < b.max_documents
    assert f.max_questions_per_month < p.max_questions_per_month < b.max_questions_per_month
    assert f.max_agents < p.max_agents < b.max_agents


def test_free_plan_has_no_paid_features():
    f = PLAN_ENTITLEMENTS[PlanName.free]
    assert not (f.live_handoff or f.lead_capture or f.menu or f.push_to_talk or f.remove_branding)


def test_only_top_plan_removes_branding():
    assert PLAN_ENTITLEMENTS[PlanName.pro_plus].remove_branding
    assert not PLAN_ENTITLEMENTS[PlanName.pro].remove_branding


def test_missing_subscription_falls_back_to_free():
    assert get_entitlements(None) == PLAN_ENTITLEMENTS[PlanName.free]
    assert entitlements_for_plan(None) == PLAN_ENTITLEMENTS[PlanName.free]


def test_restrictions_strip_paid_features_on_free():
    cfg = {"menu": [1], "behavior": {"collectEmailBeforeChat": True, "handoffToHuman": True},
           "appearance": {"showPoweredBy": False}}
    out = apply_plan_restrictions(cfg, PLAN_ENTITLEMENTS[PlanName.free])
    assert out["menu"] == [] and out["behavior"]["handoffToHuman"] is False
    assert out["behavior"]["collectEmailBeforeChat"] is False
    assert out["appearance"]["showPoweredBy"] is True


def test_restrictions_keep_features_on_business():
    cfg = {"menu": [1], "behavior": {"handoffToHuman": True}, "appearance": {"showPoweredBy": False}}
    out = apply_plan_restrictions(cfg, PLAN_ENTITLEMENTS[PlanName.pro_plus])
    assert out["menu"] == [1] and out["behavior"]["handoffToHuman"] is True
    assert out["appearance"]["showPoweredBy"] is False
