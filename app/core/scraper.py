"""
Web scraping service: turns a URL into clean Markdown (or raw PDF bytes, if
the URL points straight at a PDF file) for the knowledge base.

Pipeline for one URL (entry point: scrape_website):

  1. SSRF pre-check on the URL the tenant typed (DNS off the event loop).
  2. Resolve the URL with a plain HTTP client, following redirects one hop
     at a time and SSRF-checking (and IP-pinning — see _pin) EVERY hop
     before it is requested. This is the only reliable place to do it:
     Crawl4AI's result.url is the URL we asked for, never the one the
     browser actually landed on, so a post-crawl re-check (what the
     previous version did) never fired. Resolving here also tells us the
     final content type.
  3. If the final URL is a PDF, keep the bytes as-is — no browser needed.
  4. Otherwise render the page in Crawl4AI's headless Chromium with page
     chrome (nav/header/footer/aside/forms) excluded, the disk cache
     bypassed, and a page.route SSRF guard on every sub-request the
     rendered page itself makes (see _install_ssrf_guard); convert the
     markdown to clean text that still keeps headings and table rows (see
     _markdown_to_text), and reject bot-challenge pages ("Just a
     moment...") so they never get indexed as content.
  5. If the browser path fails for any reason, fall back to a plain HTTP
     fetch + BeautifulSoup text extraction of the same final URL.
  6. When the caller enables OCR, download the page's most relevant images
     (SSRF-checked + IP-pinned, size/dimension filtered, capped per page)
     and read the text inside them with Gemini vision (app/core/ocr.py).
     Image alt text is kept as well. This is what makes a menu photo or a
     flyer searchable.
  7. Return the cleaned text (+ image text) as UTF-8 Markdown bytes — no
     PDF round-trip. indexing_service reads .md content as plain text
     directly (see _extract_text), the same path a user's own uploaded
     .md file already takes.

Text quality matters more here than anywhere else in the app: whatever
comes out of this module is chunked, embedded and handed to the LLM as
context. Raw markdown (link targets, tooltips, menu lists) would otherwise
go straight into every chunk; _markdown_to_text strips exactly that kind
of noise while deliberately keeping headings and table structure.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import io
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

import httpx

from app.config import settings
from app.core.ip_validator import validate_url_safe

logger = logging.getLogger(__name__)


@dataclass
class ImageRef:
    """An image found on a page: where to fetch it and what the page said
    about it. `score` is Crawl4AI's relevance estimate (bigger images near
    the content score higher); the BeautifulSoup path uses document order."""
    url: str
    alt: str = ""
    score: float = 0.0


@dataclass
class _PageContent:
    text: str
    title: str
    images: list[ImageRef] = field(default_factory=list)

_MAX_REDIRECT_HOPS = 5

# Header sets for the resolve/fallback requests (the headless crawl uses
# Chromium's own). Tried in order:
#   1. Identify honestly (name + contact URL) — the polite, standard-crawler
#      norm, and the only profile robots.txt rules can actually be written
#      against by name.
#   2/3. A full browser-like set, then a bare UA, for sites that block any
#      self-identified bot outright regardless of robots.txt. Confirmed live
#      that Cloudflare on w3.org challenges the *full* Chrome-like set (403)
#      while the bare "Mozilla/5.0" sails through, whereas other hosts 403 a
#      bare client — neither wins everywhere, hence both stay as fallbacks.
_HEADER_PROFILES = (
    {
        "User-Agent": settings.SCRAPE_BOT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    },
    {"User-Agent": "Mozilla/5.0"},
)
_RETRY_STATUSES = (403, 429, 503)
# 429 (rate limited) and 503 (often "overloaded, back off") get a real wait
# + retry on the SAME header profile before falling through to switching
# profiles — switching identity does nothing for a server that's just asking
# for a slower pace. 403 skips straight to a profile switch (see below).
_BACKOFF_STATUSES = (429, 503)

# Page furniture that is never knowledge-base content. Dropped both in the
# browser crawl (Crawl4AI excluded_tags) and in the BeautifulSoup fallback.
_CHROME_TAGS = ["nav", "header", "footer", "aside", "form", "script", "style", "noscript", "iframe", "svg"]

# Elements that end a line of prose in the BeautifulSoup fallback.
_BLOCK_TAGS = [
    "p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "br", "tr", "td", "th",
    "section", "article", "blockquote", "pre", "table", "dt", "dd", "figcaption", "summary", "main",
]

# Accessibility skip-links that survive tag exclusion because they sit in
# <body> directly; pure navigation, never content.
_RE_SKIP_LINK = re.compile(r"^(skip|jump) to (main )?content$", re.IGNORECASE)

# Titles of interstitial / bot-protection pages. A crawl that "succeeds" with
# one of these has fetched the challenge, not the site — index nothing.
_BOT_CHALLENGE_MARKERS = (
    "just a moment",
    "attention required",
    "access denied",
    "verify you are human",
    "checking your browser",
    "enable javascript and cookies",
    "please wait...",
    "are you a robot",
)

# Below this many characters of prose the crawl almost certainly hit an
# empty shell / JS-only page / challenge, and the fallback is worth a try.
_MIN_TEXT_CHARS = 40

_HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "application/xml", "text/xml")

# Markers found in the raw HTML of a challenge/interstitial page even when
# its <title> doesn't say so (a differently-worded or non-English title) —
# vendor class/script names for the common anti-bot and CAPTCHA providers.
_BOT_CHALLENGE_BODY_MARKERS = (
    "cf-chl", "challenge-platform", "cf-turnstile", "g-recaptcha",
    "hcaptcha", "cf-browser-verification", "jschl-answer", "ddos-guard",
    "px-captcha", "perimeterx",
)
# A real article/doc page is mostly text; a challenge page is almost all
# markup/script around a few words ("Checking your browser..."). Below this
# text-to-HTML ratio AND a non-trivial amount of raw HTML, treat it as a
# likely challenge even with no title/body marker match.
_MIN_TEXT_TO_HTML_RATIO = 0.01
_MIN_HTML_LEN_FOR_RATIO_CHECK = 5000


def _looks_like_bot_challenge_body(html: Optional[str], text_len: int, status_code: Optional[int] = None) -> bool:
    """Body-level bot-challenge signal, in addition to the title check
    (_looks_like_bot_challenge): known vendor markers anywhere in the raw
    HTML, or a status code commonly used for bot-blocking (403/429/503)
    paired with a suspiciously low text-to-markup ratio."""
    if not html:
        return False
    low = html.lower()
    if any(marker in low for marker in _BOT_CHALLENGE_BODY_MARKERS):
        return True
    html_len = len(html)
    if (
        html_len >= _MIN_HTML_LEN_FOR_RATIO_CHECK
        and (text_len / html_len) < _MIN_TEXT_TO_HTML_RATIO
        and status_code in (403, 429, 503)
    ):
        return True
    return False


# ── Politeness: per-domain pacing, Retry-After backoff, robots.txt ──────────

_last_request_at: dict[str, float] = {}
_pacing_lock = asyncio.Lock()


async def _pace(host: str) -> None:
    """Keep at least SCRAPE_MIN_REQUEST_INTERVAL_SECONDS between two requests
    to the same domain. Process-local (this app runs a single worker — see
    deploy notes — so there is nothing to share across processes); good
    enough for the one-URL-at-a-time, occasionally-a-few-from-one-site
    volume this app actually sees."""
    interval = settings.SCRAPE_MIN_REQUEST_INTERVAL_SECONDS
    if interval <= 0 or not host:
        return
    async with _pacing_lock:
        last = _last_request_at.get(host)
        now = time.monotonic()
        if last is not None:
            wait = interval - (now - last)
            if wait > 0:
                await asyncio.sleep(wait)
        _last_request_at[host] = time.monotonic()


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """RFC 7231 Retry-After: either a delay in seconds, or an HTTP-date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


_ROBOTS_CACHE_MAX = 256
# host -> (expires_at monotonic, RobotFileParser | None). None means "no
# usable robots.txt" (missing, unreachable, or unparseable) — fail open.
_robots_cache: "OrderedDict[str, tuple[float, object]]" = OrderedDict()


async def _fetch_robots_rules(host: str, scheme: str):
    from urllib.robotparser import RobotFileParser

    robots_url = f"{scheme}://{host}/robots.txt"
    try:
        pinned_url, pin_host = await _pin(robots_url)
        headers = {**_HEADER_PROFILES[0], "Host": pin_host}
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            resp = await client.get(
                pinned_url, headers=headers, extensions={"sni_hostname": pin_host}
            )
        # A redirect target would need its own pin+SSRF check; robots.txt is
        # low-stakes enough that treating a redirect as "unavailable" (fail
        # open) is the right tradeoff against that extra complexity.
        if resp.status_code >= 400 or resp.is_redirect:
            return None
        rules = RobotFileParser()
        rules.parse(resp.text.splitlines())
        return rules
    except Exception:
        return None


async def _robots_allowed(url: str) -> bool:
    """
    True if `url` may be fetched per its domain's robots.txt, checked
    against SCRAPE_BOT_USER_AGENT (RobotFileParser falls back to the "*"
    group itself if there's no entry naming us specifically). Cached
    per-domain in-process for SCRAPE_ROBOTS_TXT_CACHE_SECONDS. Fails OPEN on
    any fetch/parse problem — no reachable or parseable robots.txt imposes
    no restriction, matching standard crawler behavior.
    """
    if not settings.SCRAPE_ROBOTS_TXT_ENABLED:
        return True
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host:
        return True

    cache_key = f"{parsed.scheme}://{host}"
    now = time.monotonic()
    cached = _robots_cache.get(cache_key)
    if cached is not None and cached[0] > now:
        _robots_cache.move_to_end(cache_key)
        rules = cached[1]
    else:
        rules = await _fetch_robots_rules(host, parsed.scheme)
        _robots_cache[cache_key] = (now + settings.SCRAPE_ROBOTS_TXT_CACHE_SECONDS, rules)
        _robots_cache.move_to_end(cache_key)
        if len(_robots_cache) > _ROBOTS_CACHE_MAX:
            _robots_cache.popitem(last=False)

    if rules is None:
        return True
    try:
        return bool(rules.can_fetch(settings.SCRAPE_BOT_USER_AGENT, url))
    except Exception:
        return True


# ── Public entry point ───────────────────────────────────────────────────────

async def scrape_website(
    url: str,
    timeout: int = 30,
    *,
    ocr: bool = False,
    ocr_usages: Optional[list] = None,
) -> Tuple[bytes, str, str, Optional[str], Optional[str]]:
    """
    Scrape `url` and return (content_bytes, page_title, mime_type, etag,
    last_modified). `timeout` is the per-request ceiling in seconds,
    enforced on every network step.

    mime_type is "application/pdf" only when the URL pointed straight at a
    PDF file (fetched as-is, no rendering). Everything else — a rendered
    page or the plain-HTTP fallback — comes back as "text/markdown": the
    cleaned text is written directly as the document's content instead of
    round-tripped through a generated PDF, which used to flatten headings
    and tables down to undifferentiated prose before indexing ever saw
    them. The indexer already reads .md content as plain UTF-8 text (see
    indexing_service._extract_text), so this needs no new ingestion path.

    etag/last_modified are whatever the page's own HTTP response sent
    (None if neither) — stored by the caller as the baseline for the
    weekly refresh sweep's cheap conditional-GET change check (see
    indexing_service._scrape_url_into_document / scrape_refresh_sweep_loop).

    ocr:        also read text inside the page's images with Gemini vision.
                The caller decides this (feature flag + the tenant's
                trial/wallet state) because every image is a billable call.
    ocr_usages: a list the GenerationUsage of every OCR call is appended
                to, so the caller can record/bill them.

    Raises ValueError for anything the caller should surface as a failed
    scrape (unsafe URL, unreachable, no content, bot-protected).
    """
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("URL must start with http:// or https://")

    await _validate_safe(url)

    final_url, content_type, pdf_bytes, etag, last_modified = await _resolve_url(url, timeout)

    # Checked against the URL we actually ended up at (post-redirect), not
    # just the one the tenant typed — a redirect can land on a different
    # domain with its own robots.txt.
    if not await _robots_allowed(final_url):
        raise ValueError(f"Blocked by {urlparse(final_url).hostname}'s robots.txt")

    if pdf_bytes is not None:
        title = _title_from_url(final_url)
        logger.info("Fetched PDF directly from %s (%d bytes)", final_url, len(pdf_bytes))
        return pdf_bytes, title, "application/pdf", etag, last_modified

    page: _PageContent
    try:
        page = await _crawl_with_browser(final_url, timeout)
    except Exception as e:
        # Crawl4AI's pipeline crashes or comes back empty on some sites
        # (bot-protected, nonstandard HTML, JS that never settles). A plain
        # fetch of the same final URL is cheap and often works.
        logger.warning("Browser crawl failed for %s (%s); trying plain-HTTP fallback", final_url, e)
        try:
            page = await _fetch_plain(final_url, timeout)
        except Exception as fallback_error:
            logger.error("Scraping failed for %s: %s", final_url, fallback_error)
            raise ValueError(f"Failed to scrape {url}: {fallback_error}") from fallback_error

    text = page.text
    image_block = ""
    if ocr and page.images:
        image_block = await _ocr_images(page.images, final_url, timeout, ocr_usages)
    elif page.images:
        image_block = _alt_text_block(page.images)
    if image_block:
        text = f"{text}\n\n{image_block}" if text else image_block

    text = _cap_text(text, final_url)
    if len(text.strip()) < _MIN_TEXT_CHARS:
        raise ValueError(f"No readable content extracted from {url}")

    title = _clean_title(page.title) or _title_from_url(final_url)
    logger.info(
        "Scraped %s - title=%r chars=%d images_found=%d ocr=%s",
        final_url, title, len(text), len(page.images), ocr,
    )

    content = f"# {title}\n\nSource: {final_url}\n\n{text}".encode("utf-8")
    return content, title, "text/markdown", etag, last_modified


# ── Step 1/2: safety + redirect resolution ───────────────────────────────────

async def _validate_safe(url: str) -> None:
    """validate_url_safe does blocking DNS; keep it off the event loop so a
    slow resolver can't stall every other request while a scrape runs."""
    await asyncio.to_thread(validate_url_safe, url)


async def _pin(url: str) -> Tuple[str, str]:
    """
    Resolve `url`'s hostname to one validated-safe IP (off the event loop)
    and return (pinned_url, original_host) — pinned_url has the hostname
    replaced by that literal IP, so the caller's HTTP client connects to the
    exact address just validated instead of re-resolving the hostname a
    moment later itself. Without this, a validated-safe lookup and the
    client's own connect-time lookup are two separate DNS queries; an
    attacker-controlled DNS server can legitimately answer "safe" on the
    first and "unsafe" (a private/internal IP) on the second — classic DNS
    rebinding. Pinning closes that gap by making there be only one lookup.

    Callers MUST send the returned original_host as both the `Host` header
    and the `sni_hostname` extension on every request made with pinned_url,
    so virtual hosting and TLS SNI still resolve to the real site rather
    than whatever default the bare IP would otherwise present.
    """
    from app.core.ip_validator import resolve_safe_ip

    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Invalid URL: no hostname")

    ip = await asyncio.to_thread(resolve_safe_ip, hostname)
    netloc = f"[{ip}]" if ":" in ip else ip  # IPv6 literal needs brackets in a URL authority
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    pinned = parsed._replace(netloc=netloc).geturl()
    return pinned, hostname


def _pinned_request_kwargs(host: str, headers: dict) -> dict:
    """Headers + extensions for a request already sent to a pinned IP (see
    _pin) — restores the Host header and TLS SNI the IP-literal URL on its
    own would otherwise lose or get wrong."""
    return {
        "headers": {**headers, "Host": host},
        "extensions": {"sni_hostname": host},
    }


def _size_cap_bytes() -> int:
    return int(settings.CRAWL4AI_MAX_CONTENT_SIZE_MB * 1024 * 1024)


async def _resolve_url(
    url: str, timeout: int
) -> Tuple[str, Optional[str], Optional[bytes], Optional[str], Optional[str]]:
    """
    Follow redirects manually, SSRF-checking each hop *before* it is
    requested. Returns (final_url, content_type, pdf_bytes, etag,
    last_modified).

    pdf_bytes is only set when the final resource is a PDF (by Content-Type
    or magic bytes) — in that case the body has been read here, size-capped,
    and the caller skips the browser entirely.

    etag/last_modified are whatever the final (non-redirect) response sent,
    regardless of content type — the weekly refresh sweep's baseline for a
    cheap conditional GET later (see indexing_service.
    _scrape_url_into_document / scrape_refresh_sweep_loop). None when the
    server sent neither, or when this step never got a final response at
    all (left to the browser crawl instead).

    Non-SSRF HTTP problems (a 403 for non-browser clients, a flaky server)
    are logged and the caller proceeds to the browser crawl with the last
    URL reached — Chromium may well succeed where a bare client didn't.
    """
    current_url = url
    profile = 0
    rate_limit_retries = 0
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
        for _ in range(_MAX_REDIRECT_HOPS + 1 + len(_HEADER_PROFILES) + settings.SCRAPE_MAX_RATE_LIMIT_RETRIES):
            pinned_url, host = await _pin(current_url)
            await _pace(host)
            try:
                async with client.stream(
                    "GET", pinned_url, **_pinned_request_kwargs(host, _HEADER_PROFILES[profile])
                ) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            return current_url, None, None, None, None
                        current_url = str(httpx.URL(current_url).join(location))
                        continue

                    if (
                        resp.status_code in _BACKOFF_STATUSES
                        and rate_limit_retries < settings.SCRAPE_MAX_RATE_LIMIT_RETRIES
                    ):
                        rate_limit_retries += 1
                        wait = min(
                            _parse_retry_after(resp.headers.get("retry-after")) or 2.0,
                            settings.SCRAPE_RATE_LIMIT_MAX_WAIT_SECONDS,
                        )
                        logger.info(
                            "rate-limited (%d) fetching %s — waiting %.1fs (retry %d/%d)",
                            resp.status_code, current_url, wait, rate_limit_retries,
                            settings.SCRAPE_MAX_RATE_LIMIT_RETRIES,
                        )
                        await asyncio.sleep(wait)
                        continue

                    if resp.status_code in _RETRY_STATUSES and profile + 1 < len(_HEADER_PROFILES):
                        profile += 1  # bot-filter disliked this header set; try the next
                        continue

                    content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                    etag = resp.headers.get("etag")
                    last_modified = resp.headers.get("last-modified")
                    declared_len = resp.headers.get("content-length")
                    if declared_len and declared_len.isdigit() and int(declared_len) > _size_cap_bytes():
                        raise ValueError(
                            f"Content is larger than the {settings.CRAWL4AI_MAX_CONTENT_SIZE_MB}MB limit"
                        )

                    # One iterator for the whole body — httpx refuses to
                    # re-iterate a stream, so the sniffed head chunk must be
                    # taken from the same iterator the PDF branch continues.
                    chunks = resp.aiter_bytes(64 * 1024)
                    head = await anext(chunks, b"")

                    is_pdf = content_type == "application/pdf" or head.startswith(b"%PDF-")
                    if not is_pdf:
                        return current_url, content_type or None, None, etag, last_modified

                    if resp.status_code >= 400:
                        raise ValueError(f"HTTP {resp.status_code} fetching {current_url}")
                    body = bytearray(head)
                    async for chunk in chunks:
                        body.extend(chunk)
                        if len(body) > _size_cap_bytes():
                            raise ValueError(
                                f"PDF is larger than the {settings.CRAWL4AI_MAX_CONTENT_SIZE_MB}MB limit"
                            )
                    return current_url, "application/pdf", bytes(body), etag, last_modified
            except ValueError:
                raise
            except httpx.HTTPError as e:
                logger.info("Resolve step could not fetch %s (%s); leaving it to the browser", current_url, e)
                return current_url, None, None, None, None
        raise ValueError(f"Too many redirects while fetching {url}")


# ── Step 4: headless browser crawl ───────────────────────────────────────────

async def _sub_request_allowed(url: str, cache: dict[str, bool]) -> bool:
    """
    The SSRF policy a rendered page's own sub-requests must pass: http(s)
    scheme only, and the hostname must resolve to a safe address. Split out
    from _install_ssrf_guard as a plain function (no Playwright Route/
    Request objects involved) so it's unit-testable on its own. `cache` is
    the caller's per-page-load hostname→verdict cache — one DNS check per
    hostname, not per sub-request.
    """
    from app.core.ip_validator import resolve_safe_ip

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname or ""
    cached = cache.get(host)
    if cached is not None:
        return cached
    try:
        await asyncio.to_thread(resolve_safe_ip, host)
        cache[host] = True
    except Exception:
        cache[host] = False
    return cache[host]


def _install_ssrf_guard(strategy) -> None:
    """
    Registers a Playwright-level page.route guard on Crawl4AI's
    `on_page_context_created` hook: every sub-request the *rendered* page
    makes (images, scripts, XHR/fetch, fonts, CSS @import, ...) is
    SSRF-checked the same way the top-level URL already is (see
    _sub_request_allowed), and any non-http(s) scheme is rejected. Without
    this, a page's own JavaScript or markup could point a request at an
    internal address and the browser would simply go there — the manual-
    redirect checks in _resolve_url/_fetch_plain only ever see requests
    this module issues itself, never ones the rendered page's own code
    makes.
    """
    cache: dict[str, bool] = {}

    async def _on_context(page, context=None, **kwargs) -> None:
        async def _guard(route) -> None:
            request = route.request
            if await _sub_request_allowed(request.url, cache):
                await route.continue_()
            else:
                logger.warning("Blocked unsafe sub-request from rendered page: %s", request.url)
                await route.abort()

        await page.route("**/*", _guard)

    strategy.set_hook("on_page_context_created", _on_context)


# ── Shared browser lifecycle ─────────────────────────────────────────────────
#
# One AsyncWebCrawler instance for the whole process instead of launching a
# fresh headless Chromium per scrape — a new browser per call is slow and
# memory-heavy on a single worker. init_shared_browser()/close_shared_browser()
# are called from app.main's lifespan; _get_browser() lazily starts one itself
# (locked, so concurrent first-callers can't start two) if that never ran,
# e.g. in a script or test that imports this module directly.

_shared_crawler = None  # type: ignore  # AsyncWebCrawler | None, lazily typed to avoid a hard import at module load
_browser_lock = asyncio.Lock()
_browser_semaphore: Optional[asyncio.Semaphore] = None

# Substrings of exceptions that mean the browser process itself is gone, not
# just that this one page failed — worth restarting the shared instance for,
# rather than leaving every future scrape to fail against a dead browser.
_BROWSER_DEAD_MARKERS = (
    "has been closed", "connection closed", "target closed",
    "browser has disconnected", "websocket connection error",
)


def _new_crawler():
    from crawl4ai import AsyncWebCrawler, BrowserConfig

    crawler = AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False))
    _install_ssrf_guard(crawler.crawler_strategy)
    return crawler


async def init_shared_browser() -> None:
    """Start the one shared browser instance. Call once from the app
    lifespan at startup; safe to call again (a no-op if already running)."""
    global _shared_crawler, _browser_semaphore
    if _browser_semaphore is None:
        _browser_semaphore = asyncio.Semaphore(max(1, settings.SCRAPE_MAX_CONCURRENT_PAGES))
    async with _browser_lock:
        if _shared_crawler is not None:
            return
        crawler = _new_crawler()
        await crawler.start()
        _shared_crawler = crawler
        logger.info("Shared scraping browser started (max %d concurrent pages)", settings.SCRAPE_MAX_CONCURRENT_PAGES)


async def close_shared_browser() -> None:
    """Stop the shared browser. Call once from the app lifespan at shutdown."""
    global _shared_crawler
    async with _browser_lock:
        if _shared_crawler is None:
            return
        try:
            await _shared_crawler.close()
        except Exception:
            logger.exception("Error closing shared scraping browser")
        _shared_crawler = None


async def _get_browser():
    """
    The shared crawler, starting it lazily (locked) if the app lifespan
    never did — keeps this module self-sufficient outside the FastAPI app
    (scripts, tests).

    Raises plainly when CRAWL4AI_ENABLED is off (Chromium isn't installed in
    this deployment, e.g. a memory-constrained free-tier image) instead of
    attempting a doomed browser launch — the caller (scrape_website) treats
    any exception here as "fall back to the plain-HTTP fetcher", but now
    with an honest reason in the log instead of a generic launch failure.
    """
    if not settings.CRAWL4AI_ENABLED:
        raise RuntimeError("CRAWL4AI_ENABLED is off — no headless browser in this deployment")
    global _shared_crawler, _browser_semaphore
    if _shared_crawler is None:
        await init_shared_browser()
    return _shared_crawler


async def _restart_shared_browser() -> None:
    global _shared_crawler
    async with _browser_lock:
        if _shared_crawler is not None:
            try:
                await _shared_crawler.close()
            except Exception:
                pass
            _shared_crawler = None
    await init_shared_browser()
    logger.warning("Shared scraping browser restarted after a crash")


async def _crawl_with_browser(url: str, timeout: int) -> _PageContent:
    """Render with Crawl4AI (the one shared browser instance, concurrency-
    capped) and return the page's prose, title and image references. Raises
    on any failure so the caller can fall back."""
    from crawl4ai import CacheMode, CrawlerRunConfig

    run_config = CrawlerRunConfig(
        # Always fetch fresh. The default (ENABLED) serves a disk cache, so a
        # tenant re-adding a URL after updating their site got stale text.
        cache_mode=CacheMode.BYPASS,
        # The old `AsyncWebCrawler(timeout=...)` kwarg was silently ignored —
        # BrowserConfig has no such field — so navigation ran on Crawl4AI's
        # 60s default regardless of CRAWL4AI_TIMEOUT. This is the real knob.
        page_timeout=int(timeout * 1000),
        excluded_tags=_CHROME_TAGS,
        remove_overlay_elements=True,
        # Scroll the whole page first so lazy-loaded sections and images
        # (product grids, "load more on scroll" content) are in the DOM.
        scan_full_page=True,
        scroll_delay=0.2,
        word_count_threshold=1,
        verbose=False,
    )

    await _pace(urlparse(url).hostname or "")
    crawler = await _get_browser()
    async with _browser_semaphore:
        try:
            # page_timeout only bounds navigation; post-processing can still
            # drag on a huge page. Hard ceiling at 2x so a scrape can't hang
            # the indexing task indefinitely.
            result = await asyncio.wait_for(crawler.arun(url, config=run_config), timeout=timeout * 2)
        except Exception as e:
            if any(m in str(e).lower() for m in _BROWSER_DEAD_MARKERS):
                logger.warning("Shared browser looks dead (%s) — restarting it", e)
                await _restart_shared_browser()
            raise

    if not result.success:
        raise ValueError(f"crawl failed: {result.error_message or 'unknown error'}")
    status = getattr(result, "status_code", None)
    if status and status >= 400:
        raise ValueError(f"HTTP {status}")

    metadata = result.metadata or {}
    title = metadata.get("title") or metadata.get("og:title") or ""
    if _looks_like_bot_challenge(title):
        raise ValueError(f"bot-protection page returned ({title.strip()!r})")

    markdown = result.markdown
    if not isinstance(markdown, str):
        # Newer Crawl4AI versions hand back a MarkdownGenerationResult here.
        markdown = getattr(markdown, "raw_markdown", None) or ""

    text = _markdown_to_text(markdown)
    images = _images_from_crawl_result(result, url)
    if len(text) < _MIN_TEXT_CHARS and not images:
        raise ValueError("page rendered but no readable text was extracted")
    if _looks_like_bot_challenge_body(getattr(result, "html", None), len(text), status):
        raise ValueError("bot-protection page returned (detected by body markers)")
    return _PageContent(text=text, title=title, images=images)


def _images_from_crawl_result(result, base_url: str) -> list[ImageRef]:
    """Crawl4AI lists every <img> (and each srcset candidate) under
    result.media['images'] with a relevance score. Keep one entry per
    group (the srcset variants share a group_id), best score first."""
    entries = (getattr(result, "media", None) or {}).get("images") or []
    seen_groups: set = set()
    images: list[ImageRef] = []
    for entry in entries:
        group = entry.get("group_id")
        if group is not None and group in seen_groups:
            continue
        abs_url = _absolute_image_url(entry.get("src") or "", base_url)
        if not abs_url:
            continue
        if group is not None:
            seen_groups.add(group)
        try:
            score = float(entry.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        images.append(ImageRef(url=abs_url, alt=(entry.get("alt") or "").strip(), score=score))
    images.sort(key=lambda i: i.score, reverse=True)
    return images


# ── Step 5: plain-HTTP fallback ──────────────────────────────────────────────

async def _fetch_plain(url: str, timeout: int) -> _PageContent:
    """httpx GET + BeautifulSoup text extraction. Redirects are followed
    manually with an SSRF check per hop, same as _resolve_url."""
    from bs4 import BeautifulSoup

    current_url = url
    profile = 0
    rate_limit_retries = 0
    status_code = None
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
        for _ in range(_MAX_REDIRECT_HOPS + 1 + len(_HEADER_PROFILES) + settings.SCRAPE_MAX_RATE_LIMIT_RETRIES):
            pinned_url, host = await _pin(current_url)
            await _pace(host)
            async with client.stream(
                "GET", pinned_url, **_pinned_request_kwargs(host, _HEADER_PROFILES[profile])
            ) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise ValueError("redirect without a Location header")
                    current_url = str(httpx.URL(current_url).join(location))
                    continue
                if (
                    resp.status_code in _BACKOFF_STATUSES
                    and rate_limit_retries < settings.SCRAPE_MAX_RATE_LIMIT_RETRIES
                ):
                    rate_limit_retries += 1
                    wait = min(
                        _parse_retry_after(resp.headers.get("retry-after")) or 2.0,
                        settings.SCRAPE_RATE_LIMIT_MAX_WAIT_SECONDS,
                    )
                    logger.info(
                        "rate-limited (%d) fetching %s — waiting %.1fs (retry %d/%d)",
                        resp.status_code, current_url, wait, rate_limit_retries,
                        settings.SCRAPE_MAX_RATE_LIMIT_RETRIES,
                    )
                    await asyncio.sleep(wait)
                    continue
                if resp.status_code in _RETRY_STATUSES and profile + 1 < len(_HEADER_PROFILES):
                    profile += 1
                    continue
                resp.raise_for_status()
                status_code = resp.status_code
                content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                if content_type and not any(content_type.startswith(t) for t in _HTML_CONTENT_TYPES):
                    raise ValueError(f"unsupported content type {content_type!r}")
                body = bytearray()
                async for chunk in resp.aiter_bytes(64 * 1024):
                    body.extend(chunk)
                    if len(body) > _size_cap_bytes():
                        raise ValueError(
                            f"page is larger than the {settings.CRAWL4AI_MAX_CONTENT_SIZE_MB}MB limit"
                        )
                encoding = resp.encoding or "utf-8"
                break
        else:
            raise ValueError(f"Too many redirects while fetching {url}")

    html = bytes(body).decode(encoding, errors="ignore")
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string
    if _looks_like_bot_challenge(title):
        raise ValueError(f"bot-protection page returned ({title.strip()!r})")

    for tag in soup(_CHROME_TAGS + ["head"]):
        tag.decompose()

    # Images in document order (nav/footer logos are already gone above).
    images: list[ImageRef] = []
    seen: set = set()
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src and img.get("srcset"):
            src = img["srcset"].split(",")[0].strip().split(" ")[0]
        abs_url = _absolute_image_url(src, current_url)
        if abs_url and abs_url not in seen:
            seen.add(abs_url)
            images.append(ImageRef(url=abs_url, alt=(img.get("alt") or "").strip()))

    # Line breaks only at block boundaries. get_text("\n") would break on
    # every inline <a>/<code>/<span> too, leaving one word per line.
    for tag in soup(_BLOCK_TAGS):
        tag.append("\n")
    lines = soup.get_text(separator=" ").splitlines()
    text = _tidy_lines(lines)
    if len(text) < _MIN_TEXT_CHARS and not images:
        raise ValueError("no readable text in the HTML")
    if _looks_like_bot_challenge_body(html, len(text), status_code):
        raise ValueError("bot-protection page returned (detected by body markers)")
    return _PageContent(text=text, title=title, images=images)


# ── Whole-site URL discovery ──────────────────────────────────────────────────
#
# Finds the pages of a site worth adding, without fetching or indexing any of
# them — that's still done one URL at a time by the existing single-page
# pipeline (schedule_url_scrape -> _scrape_url_into_document -> scrape_website
# above), so every discovered page automatically gets the exact same SSRF
# checks, robots.txt compliance, bot-challenge/CAPTCHA detection, and
# image OCR as a page added by hand — none of that is reimplemented here.

_SITEMAP_FETCH_TIMEOUT = 15
_MAX_NESTED_SITEMAPS = 5  # <sitemap> index entries to follow, at most
_MAX_BFS_FRONTIER = 500  # safety cap on how many links we'll ever remember, even past max_pages


def _same_site(url: str, host: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and (parsed.hostname or "").lower() == host.lower()


async def _discovery_get(url: str) -> Optional[str]:
    """One pinned, SSRF-safe, paced, robots.txt-respecting GET, body text
    only. None on any failure, a redirect, or a non-text response —
    discovery is best-effort throughout; a page that can't be read simply
    contributes no further links."""
    if not await _robots_allowed(url):
        return None
    try:
        pinned_url, host = await _pin(url)
        await _pace(host)
        async with httpx.AsyncClient(timeout=_SITEMAP_FETCH_TIMEOUT, follow_redirects=False) as client:
            resp = await client.get(pinned_url, **_pinned_request_kwargs(host, dict(_HEADER_PROFILES[0])))
        if resp.status_code >= 400 or resp.is_redirect:
            return None
        content_type = (resp.headers.get("content-type") or "").lower()
        if not any(t in content_type for t in ("xml", "html", "text")) and content_type:
            return None
        return resp.text
    except Exception:
        return None


async def _urls_from_sitemap(sitemap_url: str, host: str, _depth: int = 0) -> list[str]:
    """<loc> entries from a sitemap, same-domain only. A <sitemapindex>
    (a sitemap of sitemaps) is followed one level deep, capped at
    _MAX_NESTED_SITEMAPS child sitemaps."""
    from xml.etree import ElementTree

    body = await _discovery_get(sitemap_url)
    if not body:
        return []
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return []

    locs = [el.text.strip() for el in root.iter() if el.tag.lower().endswith("loc") and el.text]
    if _depth == 0 and root.tag.lower().endswith("sitemapindex"):
        urls: list[str] = []
        for loc in locs[:_MAX_NESTED_SITEMAPS]:
            if _same_site(loc, host):
                urls.extend(await _urls_from_sitemap(loc, host, _depth=1))
        return urls
    return [loc for loc in locs if _same_site(loc, host)]


async def _bfs_discover(start_url: str, host: str, max_pages: int, max_depth: int) -> list[str]:
    """Breadth-first, same-domain link-following from `start_url`, used only
    when the site has no usable sitemap. Stops once `max_pages` pages have
    been successfully read or the frontier is exhausted."""
    from bs4 import BeautifulSoup

    seen = {start_url}
    queue: list[tuple[str, int]] = [(start_url, 0)]
    found: list[str] = []
    while queue and len(found) < max_pages:
        url, depth = queue.pop(0)
        body = await _discovery_get(url)
        if body is None:
            continue
        found.append(url)
        if depth >= max_depth:
            continue
        soup = BeautifulSoup(body, "html.parser")
        for a in soup.find_all("a", href=True):
            link = urljoin(url, a["href"]).split("#")[0]
            if link not in seen and _same_site(link, host) and len(seen) < _MAX_BFS_FRONTIER:
                seen.add(link)
                queue.append((link, depth + 1))
    return found


async def discover_site_urls(start_url: str, max_pages: int, max_depth: int) -> list[str]:
    """
    URLs worth adding from the same site as `start_url` — the start URL
    itself, plus up to `max_pages` - 1 more same-domain pages, each already
    confirmed SSRF-safe and robots.txt-allowed. Tries /sitemap.xml (and the
    common /sitemap_index.xml alias) first; falls back to same-domain
    link-following up to `max_depth` only if no usable sitemap was found.

    Best-effort and tolerant throughout: an unreachable sitemap, a page that
    fails to fetch, or a disallowed URL is simply skipped rather than
    aborting the whole discovery.
    """
    parsed = urlparse(start_url)
    host = parsed.hostname or ""
    if not host:
        return [start_url]

    urls: list[str] = []
    for sitemap_path in ("/sitemap.xml", "/sitemap_index.xml"):
        urls = await _urls_from_sitemap(f"{parsed.scheme}://{host}{sitemap_path}", host)
        if urls:
            logger.info("Site crawl: found %d URL(s) via %s%s", len(urls), host, sitemap_path)
            break

    if not urls:
        logger.info("Site crawl: no usable sitemap for %s — falling back to link-following", host)
        urls = await _bfs_discover(start_url, host, max_pages, max_depth)

    # Final pass: always lead with the URL the tenant actually typed, dedupe,
    # cap at max_pages, and robots.txt-check every candidate individually
    # (the sitemap path never checked per-URL; cheap to redo for BFS results
    # since _robots_allowed is cached per domain).
    ordered = [start_url] + [u for u in urls if u != start_url]
    allowed: list[str] = []
    for url in ordered:
        if len(allowed) >= max_pages:
            break
        if await _robots_allowed(url):
            allowed.append(url)
    return allowed


# ── Weekly refresh: cheap "did this page change?" probe ─────────────────────

async def check_for_update(
    url: str, etag: Optional[str], last_modified: Optional[str], timeout: int = 15
) -> Optional[dict]:
    """
    A conditional, lightweight probe for the weekly refresh sweep — NOT a
    full scrape (no browser render, no OCR, no markdown cleanup). Sends
    If-None-Match/If-Modified-Since when a prior value is known; a 304
    response is the cheapest possible "unchanged" and reads no body at all.

    Returns None on any failure, a now-disallowed robots.txt, or a redirect
    (the sweep leaves the document alone and tries again next cycle — a
    transient refresh-check problem must never flip a working document to
    failed; only an actual re-scrape attempt does that). Otherwise returns
    {"not_modified": bool, "etag": str|None, "last_modified": str|None,
    "content_hash": str|None}. content_hash is only set on a 200 (the body
    was actually read) — the caller compares it against the document's
    stored hash as the fallback check for sites that ignore conditional
    headers entirely and just always return 200.
    """
    import hashlib

    if not await _robots_allowed(url):
        return None
    try:
        pinned_url, host = await _pin(url)
        await _pace(host)
        headers = dict(_HEADER_PROFILES[0])
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.get(pinned_url, **_pinned_request_kwargs(host, headers))
        if resp.status_code == 304:
            return {"not_modified": True, "etag": etag, "last_modified": last_modified, "content_hash": None}
        if resp.status_code >= 400 or resp.is_redirect:
            return None
        return {
            "not_modified": False,
            "etag": resp.headers.get("etag"),
            "last_modified": resp.headers.get("last-modified"),
            "content_hash": hashlib.sha256(resp.content).hexdigest(),
        }
    except Exception:
        return None


# ── Images: alt text + OCR ───────────────────────────────────────────────────

_IMAGE_EXT_SKIP = (".svg", ".gif", ".ico", ".webm", ".mp4")
_IMAGE_MIME_OK = ("image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp", "image/tiff")


def _absolute_image_url(src: str, base_url: str) -> Optional[str]:
    src = (src or "").strip()
    if not src or src.startswith("data:"):
        return None
    # Crawl4AI's srcset parsing can hand back "http//host/x" (missing colon).
    src = re.sub(r"^(https?)//", r"\1://", src)
    abs_url = urljoin(base_url, src)
    parsed = urlparse(abs_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if parsed.path.lower().endswith(_IMAGE_EXT_SKIP):
        return None
    return abs_url


def _meaningful_alt(alt: str) -> bool:
    alt = (alt or "").strip()
    return len(alt) >= 4 and not re.fullmatch(r"[\w-]+\.(png|jpe?g|webp|gif)", alt, re.IGNORECASE)


def _alt_text_block(images: list[ImageRef]) -> str:
    """Without OCR the alt attributes are all we know about the pictures;
    keep the descriptive ones (a stripped '![alt](src)' loses them)."""
    lines, seen = [], set()
    for image in images:
        if _meaningful_alt(image.alt) and image.alt not in seen:
            seen.add(image.alt)
            lines.append(f"Image: {image.alt}")
    return "\n".join(lines)


async def _ocr_images(
    images: list[ImageRef], page_url: str, timeout: int, usages: Optional[list]
) -> str:
    """Download the top images and read their text with Gemini. Every
    failure is per-image (logged, skipped) — a bad CDN link or an OCR
    timeout must not fail the whole page. Returns a text block, or ''."""
    from app.core.ocr import extract_text_from_image

    candidates = images[: settings.SCRAPE_OCR_MAX_IMAGES]
    semaphore = asyncio.Semaphore(max(1, settings.SCRAPE_OCR_CONCURRENCY))

    async def _one(image: ImageRef) -> Optional[str]:
        async with semaphore:
            try:
                data, mime = await _download_image(image.url, page_url, timeout)
                if data is None:
                    return None
                text, usage = await extract_text_from_image(data, mime)
                if usage is not None and usages is not None:
                    usages.append(usage)
            except Exception as e:
                logger.info("OCR skipped for %s: %s", image.url, e)
                return None
        parts = []
        if _meaningful_alt(image.alt):
            parts.append(f"Image: {image.alt}")
        if text:
            parts.append(text)
        return "\n".join(parts) if parts else None

    results = await asyncio.gather(*(_one(img) for img in candidates))
    blocks = [r for r in results if r]
    # Alt text for the images we didn't OCR (past the cap) is still worth keeping.
    rest_alt = _alt_text_block(images[len(candidates):])
    if rest_alt:
        blocks.append(rest_alt)
    if not blocks:
        return ""
    logger.info("OCR read text from %d/%d images on %s", len([r for r in results if r]), len(candidates), page_url)
    return "TEXT FROM IMAGES:\n" + "\n\n".join(blocks)


async def _download_image(url: str, page_url: str, timeout: int) -> Tuple[Optional[bytes], str]:
    """Fetch one image with the same SSRF/size discipline as pages. Returns
    (None, '') for anything not worth OCR-ing (too small, not an image)."""
    cap = int(settings.SCRAPE_OCR_MAX_IMAGE_MB * 1024 * 1024)
    headers = dict(_HEADER_PROFILES[0])
    headers["Accept"] = "image/*,*/*;q=0.5"
    headers["Referer"] = page_url
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
        current = url
        for _ in range(_MAX_REDIRECT_HOPS + 1):
            pinned_url, host = await _pin(current)
            async with client.stream("GET", pinned_url, **_pinned_request_kwargs(host, headers)) as resp:
                if resp.is_redirect and resp.headers.get("location"):
                    current = str(httpx.URL(current).join(resp.headers["location"]))
                    continue
                if resp.status_code >= 400:
                    return None, ""
                mime = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > cap:
                    return None, ""
                body = bytearray()
                async for chunk in resp.aiter_bytes(64 * 1024):
                    body.extend(chunk)
                    if len(body) > cap:
                        return None, ""
                break
        else:
            return None, ""

    data = bytes(body)
    mime = mime if mime in _IMAGE_MIME_OK else _sniff_image_mime(data)
    if not mime:
        return None, ""
    if not _image_big_enough(data):
        return None, ""
    return data, mime


def _sniff_image_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return ""


def _image_big_enough(data: bytes) -> bool:
    """Icons, spacers and tracking pixels carry no text; skip anything with
    a side under SCRAPE_OCR_MIN_IMAGE_PX. Unreadable → keep (let OCR decide)."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
    except Exception:
        return True
    m = settings.SCRAPE_OCR_MIN_IMAGE_PX
    return w >= m and h >= m


# ── Text cleanup ─────────────────────────────────────────────────────────────

_RE_CODE_FENCE = re.compile(r"^\s*```.*$", re.MULTILINE)
_RE_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_RE_STASH = re.compile(r"\x00(\d+)\x00")
_RE_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
# Link text may itself contain one bracket pair ("[[26]](#cite_note-26)").
_RE_LINK = re.compile(r"\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\((?:[^()\s]|\([^()]*\))*(?:\s+\"[^\"]*\")?\)")
# Wikipedia-style footnote markers and editorial tags — noise for retrieval.
_RE_FOOTNOTE = re.compile(r"\[(?:\d{1,4}|[a-z]|citation needed|clarification needed|when\?|who\?)\]", re.IGNORECASE)
_RE_REF_LINK = re.compile(r"\[([^\[\]]+)\]\[[^\]]*\]")
_RE_AUTOLINK = re.compile(r"<(https?://[^>\s]+)>")
_RE_HTML_TAG = re.compile(r"<[^>]+>")
_RE_BLOCKQUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_RE_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_RE_HR = re.compile(r"^\s*([-*_]\s*){3,}$", re.MULTILINE)
_RE_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$", re.MULTILINE)
_RE_STRONG = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
# "__init__" is a Python name, not bold — require a non-word boundary.
_RE_STRONG_US = re.compile(r"(?<![\w_])__(?=\S)(.+?)(?<=\S)__(?![\w_])")
_RE_EMPH = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
# Underscore emphasis only when not glued to word characters, so
# snake_case_identifiers are left alone.
_RE_EMPH_US = re.compile(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
_RE_BARE_URL_LINE = re.compile(r"^\s*(?:https?://|www\.)\S+\s*$")
_RE_WS = re.compile(r"[ \t ]+")


def _markdown_to_text(markdown: str) -> str:
    """
    Reduce Crawl4AI markdown to clean, structured prose — not plain text.
    Link *text* is kept, link *targets*/tooltips/images/emphasis markers/
    footnote markers/bare URLs are dropped, but `#` heading markers and
    table rows (pipes) are deliberately KEPT: flattening them to plain
    prose loses exactly the structure (what's a heading vs. body text,
    what's a row vs. a column) that makes a chunk answerable later. Only
    the separator row of a table ("---|---") is removed — it carries no
    content, just alignment syntax.
    """
    text = markdown or ""
    text = _RE_CODE_FENCE.sub("", text)

    # Inline code is shown verbatim: stash spans so "`__init__`", "`<T>`"
    # or "`a*b`" aren't mangled by the emphasis/tag/link passes below.
    stashed: list[str] = []

    def _stash(match: "re.Match[str]") -> str:
        stashed.append(match.group(1))
        return f"\x00{len(stashed) - 1}\x00"

    text = _RE_INLINE_CODE.sub(_stash, text)
    text = _RE_IMAGE.sub("", text)
    # Links can nest in odd ways on real pages; two passes catch the common
    # "[**text**](url)" and "[[inner](u1)](u2)" shapes.
    for _ in range(2):
        text = _RE_LINK.sub(r"\1", text)
    text = _RE_REF_LINK.sub(r"\1", text)
    text = _RE_AUTOLINK.sub("", text)
    text = _RE_HTML_TAG.sub("", text)
    text = _RE_TABLE_SEP.sub("", text)
    text = _RE_HR.sub("", text)
    text = _RE_BLOCKQUOTE.sub("", text)
    text = _RE_BULLET.sub("", text)
    text = _RE_STRONG.sub(r"\1", text)
    text = _RE_STRONG_US.sub(r"\1", text)
    text = _RE_EMPH.sub(r"\1", text)
    text = _RE_EMPH_US.sub(r"\1", text)
    text = _RE_FOOTNOTE.sub("", text)
    text = html_lib.unescape(text)
    text = _RE_STASH.sub(lambda m: stashed[int(m.group(1))], text)
    return _tidy_lines(text.splitlines())


def _tidy_lines(lines: list[str]) -> str:
    """Collapse whitespace, drop URL-only / punctuation-only lines and
    immediate duplicates, and keep paragraph breaks as single blank lines."""
    out: list[str] = []
    last = None
    for raw in lines:
        line = _RE_WS.sub(" ", raw).strip()
        if line and (
            _RE_BARE_URL_LINE.match(line)
            or _RE_SKIP_LINK.match(line)
            or not re.search(r"[A-Za-z0-9\u0080-￿]", line)
        ):
            continue
        if line and line == last:
            continue
        if not line and (not out or out[-1] == ""):
            continue
        out.append(line)
        last = line or last
    return "\n".join(out).strip()


def _looks_like_bot_challenge(title: Optional[str]) -> bool:
    t = (title or "").strip().lower()
    return any(marker in t for marker in _BOT_CHALLENGE_MARKERS)


def _clean_title(title: Optional[str]) -> str:
    t = _RE_WS.sub(" ", html_lib.unescape(title or "")).strip()
    return t[:200]


def _title_from_url(url: str) -> str:
    """Human-ish title when the page has none: the last path segment
    ('pricing', 'dummy' for dummy.pdf) or the hostname."""
    parsed = urlparse(url)
    segment = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1]) if parsed.path else ""
    stem, ext = os.path.splitext(segment)
    # Only strip a real file extension — "1706.03762" (an arXiv id) is not
    # "1706" with a ".03762" extension.
    if ext.lower() in _KNOWN_EXTENSIONS:
        segment = stem
    segment = segment.replace("-", " ").replace("_", " ").strip()
    return segment or (parsed.hostname or "Untitled")


_KNOWN_EXTENSIONS = {".pdf", ".html", ".htm", ".php", ".asp", ".aspx", ".jsp", ".txt", ".md"}


def _cap_text(text: str, url: str) -> str:
    cap = _size_cap_bytes()
    if len(text.encode("utf-8", errors="ignore")) > cap:
        logger.warning("Truncating scraped text for %s to %dMB", url, settings.CRAWL4AI_MAX_CONTENT_SIZE_MB)
        return text.encode("utf-8", errors="ignore")[:cap].decode("utf-8", errors="ignore")
    return text
