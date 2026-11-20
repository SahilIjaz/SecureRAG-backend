"""
Scraper (app/core/scraper.py) and its SSRF/DNS-pinning plumbing
(app/core/ip_validator.py).

Pure cleanup/title/challenge-detection tests run anywhere. The network-shaped
ones use httpx.MockTransport (no real sockets) with DNS resolution
monkeypatched, so they're fast and hermetic.

Run with:  PYTHONPATH=. ./venv/bin/pytest tests/test_scraper.py -v
"""

from __future__ import annotations

import time

import httpx
import pytest

from app.core import scraper
from app.core.ip_validator import DnsResolutionError


# ── Markdown cleanup keeps structure, drops noise ────────────────────────────

def test_headings_are_kept():
    out = scraper._markdown_to_text("# Pricing\n\nSome intro text.\n\n## Starter plan\n\nDetails here.")
    assert "# Pricing" in out
    assert "## Starter plan" in out


def test_table_rows_are_kept_but_separator_row_is_dropped():
    md = "| Plan | Price |\n| --- | --- |\n| Starter | $10 |\n| Growth | $22 |"
    out = scraper._markdown_to_text(md)
    assert "| Plan | Price |" in out
    assert "| Starter | $10 |" in out
    assert "---" not in out


def test_links_keep_text_drop_target():
    out = scraper._markdown_to_text("Read our [pricing page](https://example.com/pricing) for details.")
    assert "pricing page" in out
    assert "example.com" not in out


def test_images_are_stripped():
    out = scraper._markdown_to_text("Before ![a logo](https://example.com/logo.png) after.")
    assert "logo.png" not in out
    assert "Before" in out and "after" in out


def test_emphasis_markers_are_stripped_but_text_kept():
    out = scraper._markdown_to_text("This is **very** important and *also* this.")
    assert "**" not in out and out.count("*") == 0
    assert "very" in out and "also" in out


def test_inline_code_is_preserved_verbatim():
    out = scraper._markdown_to_text("Call `__init__(self, *args)` to set it up.")
    assert "__init__(self, *args)" in out


def test_footnote_markers_are_stripped():
    out = scraper._markdown_to_text("The policy changed recently[12] according to staff[a].")
    assert "[12]" not in out and "[a]" not in out
    assert "The policy changed recently" in out


def test_bare_url_only_lines_are_dropped():
    out = scraper._markdown_to_text("Real content here.\n\nhttps://example.com/track?utm=1\n\nMore content.")
    assert "utm=1" not in out
    assert "Real content here." in out and "More content." in out


def test_sibling_table_rows_do_not_collapse_into_each_other():
    """Distinct rows must stay distinguishable (not mashed into one blob
    with no column separation) — this is the whole point of keeping pipes."""
    md = "| Basic | 10 |\n| Pro | 20 |"
    out = scraper._markdown_to_text(md)
    lines = [l for l in out.splitlines() if l.strip()]
    assert any("Basic" in l and "10" in l for l in lines)
    assert any("Pro" in l and "20" in l for l in lines)


# ── Bot-challenge detection ───────────────────────────────────────────────────

@pytest.mark.parametrize("title", [
    "Just a moment...", "Attention Required! | Cloudflare", "Access denied",
    "Please verify you are human", "Checking your browser before accessing",
])
def test_bot_challenge_titles_are_detected(title):
    assert scraper._looks_like_bot_challenge(title) is True


@pytest.mark.parametrize("title", ["Pricing — Acme Inc", "Home", "", None])
def test_normal_titles_are_not_flagged(title):
    assert scraper._looks_like_bot_challenge(title) is False


# ── _title_from_url ───────────────────────────────────────────────────────────

def test_title_from_url_strips_known_extension():
    assert scraper._title_from_url("https://example.com/docs/dummy.pdf") == "dummy"


def test_title_from_url_keeps_dotted_non_extension_segment():
    # An arXiv-style id must not be treated as "1706" + extension ".03762".
    assert scraper._title_from_url("https://arxiv.org/abs/1706.03762") == "1706.03762"


def test_title_from_url_falls_back_to_hostname():
    assert scraper._title_from_url("https://example.com/") == "example.com"


# ── DNS pinning (app/core/ip_validator.resolve_safe_ip + scraper._pin) ──────

@pytest.mark.asyncio
async def test_pin_replaces_hostname_with_resolved_ip(monkeypatch):
    monkeypatch.setattr("app.core.ip_validator.resolve_safe_ip", lambda host: "93.184.216.34")
    pinned, host = await scraper._pin("https://example.com/path?x=1")
    assert pinned == "https://93.184.216.34/path?x=1"
    assert host == "example.com"


@pytest.mark.asyncio
async def test_pin_preserves_explicit_port(monkeypatch):
    monkeypatch.setattr("app.core.ip_validator.resolve_safe_ip", lambda host: "93.184.216.34")
    pinned, host = await scraper._pin("https://example.com:8443/path")
    assert pinned == "https://93.184.216.34:8443/path"
    assert host == "example.com"


@pytest.mark.asyncio
async def test_pin_brackets_ipv6_literal(monkeypatch):
    monkeypatch.setattr("app.core.ip_validator.resolve_safe_ip", lambda host: "2606:2800:220:1:248:1893:25c8:1946")
    pinned, host = await scraper._pin("https://example.com/")
    assert pinned.startswith("https://[2606:2800:220:1:248:1893:25c8:1946]/")


@pytest.mark.asyncio
async def test_pin_rejects_unsafe_resolved_address(monkeypatch):
    def fake_resolve(host):
        raise ValueError("URL resolves to blocked/private IP. Cannot scrape internal services: evil.example")

    monkeypatch.setattr("app.core.ip_validator.resolve_safe_ip", fake_resolve)
    with pytest.raises(ValueError, match="blocked/private"):
        await scraper._pin("https://evil.example/")


@pytest.mark.asyncio
async def test_pin_surfaces_dns_failure_distinctly(monkeypatch):
    def fake_resolve(host):
        raise DnsResolutionError(f"Could not resolve hostname: {host}")

    monkeypatch.setattr("app.core.ip_validator.resolve_safe_ip", fake_resolve)
    with pytest.raises(DnsResolutionError):
        await scraper._pin("https://this-does-not-exist.invalid/")


# ── _sub_request_allowed (the rendered-page SSRF guard's decision logic) ────

@pytest.mark.asyncio
async def test_sub_request_rejects_non_http_scheme():
    cache: dict = {}
    assert await scraper._sub_request_allowed("data:text/html,hi", cache) is False
    assert await scraper._sub_request_allowed("file:///etc/passwd", cache) is False


@pytest.mark.asyncio
async def test_sub_request_allows_safe_host_and_caches_it(monkeypatch):
    calls = {"n": 0}

    def fake_resolve(host):
        calls["n"] += 1
        return "93.184.216.34"

    monkeypatch.setattr("app.core.ip_validator.resolve_safe_ip", fake_resolve)
    cache: dict = {}
    assert await scraper._sub_request_allowed("https://cdn.example.com/a.js", cache) is True
    assert await scraper._sub_request_allowed("https://cdn.example.com/b.png", cache) is True
    # Second request to the SAME host must not re-resolve — one DNS check per
    # hostname per page load, not per sub-request.
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_sub_request_blocks_unsafe_host(monkeypatch):
    def fake_resolve(host):
        raise ValueError("blocked")

    monkeypatch.setattr("app.core.ip_validator.resolve_safe_ip", fake_resolve)
    cache: dict = {}
    assert await scraper._sub_request_allowed("https://169.254.169.254/latest/meta-data/", cache) is False


# ── _resolve_url via httpx.MockTransport (redirect-into-private-IP, size cap) ─

def _client_factory(monkeypatch, handler, resolve_map: dict[str, str]):
    """Patch httpx.AsyncClient (as seen by app.core.scraper) to always use a
    MockTransport, and patch resolve_safe_ip to a fixed table instead of
    doing real DNS — both hermetic, no real network/sockets involved."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(scraper.httpx, "AsyncClient", factory)

    def fake_resolve(host):
        if host not in resolve_map:
            raise ValueError(f"URL resolves to blocked/private IP. Cannot scrape internal services: {host}")
        return resolve_map[host]

    monkeypatch.setattr("app.core.ip_validator.resolve_safe_ip", fake_resolve)


@pytest.mark.asyncio
async def test_resolve_url_follows_redirect_to_safe_host(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "1.2.3.4":  # old-site.example
            return httpx.Response(301, headers={"Location": "https://new-site.example/page"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>hi</html>")

    _client_factory(monkeypatch, handler, {"old-site.example": "1.2.3.4", "new-site.example": "5.6.7.8"})
    final_url, content_type, pdf_bytes, etag, last_modified = await scraper._resolve_url("https://old-site.example/", timeout=5)
    assert final_url == "https://new-site.example/page"
    assert pdf_bytes is None


@pytest.mark.asyncio
async def test_resolve_url_rejects_redirect_into_private_ip(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"Location": "http://internal.example/admin"})

    _client_factory(monkeypatch, handler, {"public.example": "1.2.3.4"})
    # internal.example is deliberately absent from the resolve map, so
    # fake_resolve raises for it — simulating a redirect that points at an
    # address the pin step refuses to connect to.
    with pytest.raises(ValueError, match="blocked/private"):
        await scraper._resolve_url("https://public.example/", timeout=5)


@pytest.mark.asyncio
async def test_resolve_url_enforces_size_cap(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": str(10 * 1024 * 1024 * 1024)})

    _client_factory(monkeypatch, handler, {"big.example": "1.2.3.4"})
    with pytest.raises(ValueError, match="larger than"):
        await scraper._resolve_url("https://big.example/huge.bin", timeout=5)


@pytest.mark.asyncio
async def test_resolve_url_detects_pdf_by_magic_bytes_even_without_header(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"%PDF-1.4 fake pdf content")

    _client_factory(monkeypatch, handler, {"files.example": "1.2.3.4"})
    final_url, content_type, pdf_bytes, etag, last_modified = await scraper._resolve_url("https://files.example/report", timeout=5)
    assert content_type == "application/pdf"
    assert pdf_bytes == b"%PDF-1.4 fake pdf content"


# ── _fetch_plain via httpx.MockTransport (40-char rule, bot challenge) ──────

@pytest.mark.asyncio
async def test_fetch_plain_rejects_short_content(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html><body>Hi</body></html>")

    _client_factory(monkeypatch, handler, {"thin.example": "1.2.3.4"})
    with pytest.raises(ValueError, match="no readable text"):
        await scraper._fetch_plain("https://thin.example/", timeout=5)


@pytest.mark.asyncio
async def test_fetch_plain_rejects_bot_challenge_page(monkeypatch):
    html = "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

    _client_factory(monkeypatch, handler, {"guarded.example": "1.2.3.4"})
    with pytest.raises(ValueError, match="bot-protection"):
        await scraper._fetch_plain("https://guarded.example/", timeout=5)


@pytest.mark.asyncio
async def test_fetch_plain_extracts_real_content_and_images(monkeypatch):
    html = (
        "<html><head><title>Acme Pricing</title></head><body>"
        "<nav>skip this nav link</nav>"
        "<h1>Pricing</h1><p>Our Starter plan costs ten dollars a month, billed monthly to your card.</p>"
        '<img src="/hero.png" alt="Team photo of the Acme staff">'
        "</body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

    _client_factory(monkeypatch, handler, {"acme.example": "1.2.3.4"})
    page = await scraper._fetch_plain("https://acme.example/pricing", timeout=5)
    assert page.title == "Acme Pricing"
    assert "Starter plan" in page.text
    assert "skip this nav link" not in page.text
    assert any("hero.png" in img.url for img in page.images)


# ── Retry-After parsing ──────────────────────────────────────────────────────

def test_parse_retry_after_numeric_seconds():
    assert scraper._parse_retry_after("12") == 12.0


def test_parse_retry_after_http_date():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    future = datetime.now(timezone.utc) + timedelta(seconds=30)
    seconds = scraper._parse_retry_after(format_datetime(future))
    assert seconds is not None and 25 <= seconds <= 31


@pytest.mark.parametrize("value", [None, "", "not-a-date-or-number"])
def test_parse_retry_after_invalid_returns_none(value):
    assert scraper._parse_retry_after(value) is None


# ── Stronger bot-challenge body detection ────────────────────────────────────

def test_body_marker_detects_cloudflare_turnstile():
    html = "<html><body><div class='cf-turnstile' data-sitekey='x'></div></body></html>"
    assert scraper._looks_like_bot_challenge_body(html, text_len=10) is True


def test_body_marker_ignores_normal_page():
    html = "<html><body>" + ("<p>Real article content. </p>" * 50) + "</body></html>"
    text_len = len("Real article content. " * 50)
    assert scraper._looks_like_bot_challenge_body(html, text_len=text_len) is False


def test_low_text_to_html_ratio_with_blocking_status_is_flagged():
    html = "<div>" * 2000 + "Access restricted" + "</div>" * 2000  # huge markup, tiny text
    assert scraper._looks_like_bot_challenge_body(html, text_len=17, status_code=403) is True


def test_low_text_to_html_ratio_without_blocking_status_is_not_flagged():
    # Same shape, but a normal 200 — a legitimately markup-heavy page (a
    # widget, an embed) must not be flagged just for being markup-heavy.
    html = "<div>" * 2000 + "Access restricted" + "</div>" * 2000
    assert scraper._looks_like_bot_challenge_body(html, text_len=17, status_code=200) is False


# ── robots.txt ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_robots_txt_blocks_disallowed_path(monkeypatch):
    robots_txt = "User-agent: *\nDisallow: /private/\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=robots_txt)

    scraper._robots_cache.clear()
    _client_factory(monkeypatch, handler, {"site.example": "1.2.3.4"})
    assert await scraper._robots_allowed("https://site.example/public/page") is True
    assert await scraper._robots_allowed("https://site.example/private/secret") is False


@pytest.mark.asyncio
async def test_robots_txt_fetch_failure_fails_open(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    scraper._robots_cache.clear()
    _client_factory(monkeypatch, handler, {"no-robots.example": "1.2.3.4"})
    assert await scraper._robots_allowed("https://no-robots.example/anything") is True


@pytest.mark.asyncio
async def test_robots_txt_is_cached_per_domain(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text="User-agent: *\nDisallow:\n")

    scraper._robots_cache.clear()
    _client_factory(monkeypatch, handler, {"cached.example": "1.2.3.4"})
    await scraper._robots_allowed("https://cached.example/a")
    await scraper._robots_allowed("https://cached.example/b")
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_robots_txt_disabled_always_allows(monkeypatch):
    monkeypatch.setattr(scraper.settings, "SCRAPE_ROBOTS_TXT_ENABLED", False)
    assert await scraper._robots_allowed("https://anything.example/private/x") is True


# ── Per-domain pacing ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pace_enforces_minimum_interval(monkeypatch):
    monkeypatch.setattr(scraper.settings, "SCRAPE_MIN_REQUEST_INTERVAL_SECONDS", 0.2)
    scraper._last_request_at.clear()
    t0 = time.monotonic()
    await scraper._pace("paced.example")
    await scraper._pace("paced.example")
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.18  # small tolerance for scheduling jitter


@pytest.mark.asyncio
async def test_pace_disabled_does_not_wait(monkeypatch):
    monkeypatch.setattr(scraper.settings, "SCRAPE_MIN_REQUEST_INTERVAL_SECONDS", 0)
    scraper._last_request_at.clear()
    t0 = time.monotonic()
    await scraper._pace("unpaced.example")
    await scraper._pace("unpaced.example")
    assert time.monotonic() - t0 < 0.1


# ── Rate-limit (429/503) backoff ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_url_retries_after_429_then_succeeds(monkeypatch):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>ok</html>")

    _client_factory(monkeypatch, handler, {"limited.example": "1.2.3.4"})
    final_url, content_type, pdf_bytes, etag, last_modified = await scraper._resolve_url("https://limited.example/", timeout=5)
    assert attempts["n"] == 2
    assert final_url == "https://limited.example/"


@pytest.mark.asyncio
async def test_resolve_url_falls_through_to_profile_switch_after_retries_exhausted(monkeypatch):
    """After SCRAPE_MAX_RATE_LIMIT_RETRIES waits, a persistent 429 should
    fall through to a header-profile switch rather than looping forever."""
    monkeypatch.setattr(scraper.settings, "SCRAPE_MAX_RATE_LIMIT_RETRIES", 1)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        ua = request.headers.get("host")  # pinned host header, not UA, but good enough as a marker
        if attempts["n"] <= 2:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>ok</html>")

    _client_factory(monkeypatch, handler, {"stubborn.example": "1.2.3.4"})
    final_url, content_type, pdf_bytes, etag, last_modified = await scraper._resolve_url("https://stubborn.example/", timeout=5)
    # 1 initial + 1 backoff retry + 1 profile-switch attempt = 3
    assert attempts["n"] == 3


# ── _get_browser / CRAWL4AI_ENABLED gate ─────────────────────────────────────

@pytest.mark.asyncio
async def test_get_browser_raises_clearly_when_disabled(monkeypatch):
    monkeypatch.setattr(scraper.settings, "CRAWL4AI_ENABLED", False)
    with pytest.raises(RuntimeError, match="CRAWL4AI_ENABLED"):
        await scraper._get_browser()
