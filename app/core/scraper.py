"""
Web scraping service: turns a URL into a PDF for the knowledge base.

Pipeline for one URL (entry point: scrape_website_to_pdf):

  1. SSRF pre-check on the URL the tenant typed (DNS off the event loop).
  2. Resolve the URL with a plain HTTP client, following redirects one hop
     at a time and SSRF-checking EVERY hop before it is requested. This is
     the only reliable place to do it: Crawl4AI's result.url is the URL we
     asked for, never the one the browser actually landed on, so a
     post-crawl re-check (what the previous version did) never fired.
     Resolving here also tells us the final content type.
  3. If the final URL is a PDF, keep the bytes as-is — no browser needed.
  4. Otherwise render the page in Crawl4AI's headless Chromium with page
     chrome (nav/header/footer/aside/forms) excluded and the disk cache
     bypassed, convert the markdown to clean prose, and reject bot-challenge
     pages ("Just a moment...") so they never get indexed as content.
  5. If the browser path fails for any reason, fall back to a plain HTTP
     fetch + BeautifulSoup text extraction of the same final URL.
  6. When the caller enables OCR, download the page's most relevant images
     (SSRF-checked, size/dimension filtered, capped per page) and read the
     text inside them with Gemini vision (app/core/ocr.py). Image alt text
     is kept as well. This is what makes a menu photo or a flyer searchable.
  7. Write the prose (+ image text) to a PDF with ReportLab.

Text quality matters more here than anywhere else in the app: whatever
comes out of this module is chunked, embedded and handed to the LLM as
context. Raw markdown (link targets, tooltips, menu lists) used to go
straight into the PDF and therefore into every chunk; _markdown_to_text
strips all of that down to the words a reader would actually see.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import io
import logging
import os
import re
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
# Chromium's own). Tried in order: a full browser-like set first, then a
# bare UA. Neither wins everywhere — confirmed live that Cloudflare on
# w3.org challenges the *full* Chrome-like set (403) while the bare
# "Mozilla/5.0" sails through, whereas other hosts 403 a bare client.
_HEADER_PROFILES = (
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


# ── Public entry point ───────────────────────────────────────────────────────

async def scrape_website_to_pdf(
    url: str,
    timeout: int = 30,
    *,
    ocr: bool = False,
    ocr_usages: Optional[list] = None,
) -> Tuple[bytes, str]:
    """
    Scrape `url` and return (pdf_bytes, page_title). `timeout` is the
    per-request ceiling in seconds and is enforced on every network step.

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

    final_url, content_type, pdf_bytes = await _resolve_url(url, timeout)

    if pdf_bytes is not None:
        title = _title_from_url(final_url)
        logger.info("Fetched PDF directly from %s (%d bytes)", final_url, len(pdf_bytes))
        return pdf_bytes, title

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

    pdf = await asyncio.to_thread(_text_to_pdf_blocking, text, title, final_url)
    return pdf, title


# ── Step 1/2: safety + redirect resolution ───────────────────────────────────

async def _validate_safe(url: str) -> None:
    """validate_url_safe does blocking DNS; keep it off the event loop so a
    slow resolver can't stall every other request while a scrape runs."""
    await asyncio.to_thread(validate_url_safe, url)


def _size_cap_bytes() -> int:
    return int(settings.CRAWL4AI_MAX_CONTENT_SIZE_MB * 1024 * 1024)


async def _resolve_url(url: str, timeout: int) -> Tuple[str, Optional[str], Optional[bytes]]:
    """
    Follow redirects manually, SSRF-checking each hop *before* it is
    requested. Returns (final_url, content_type, pdf_bytes).

    pdf_bytes is only set when the final resource is a PDF (by Content-Type
    or magic bytes) — in that case the body has been read here, size-capped,
    and the caller skips the browser entirely.

    Non-SSRF HTTP problems (a 403 for non-browser clients, a flaky server)
    are logged and the caller proceeds to the browser crawl with the last
    URL reached — Chromium may well succeed where a bare client didn't.
    """
    current_url = url
    profile = 0
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
        for _ in range(_MAX_REDIRECT_HOPS + 1 + len(_HEADER_PROFILES)):
            await _validate_safe(current_url)
            try:
                async with client.stream("GET", current_url, headers=_HEADER_PROFILES[profile]) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            return current_url, None, None
                        current_url = str(httpx.URL(current_url).join(location))
                        continue

                    if resp.status_code in _RETRY_STATUSES and profile + 1 < len(_HEADER_PROFILES):
                        profile += 1  # bot-filter disliked this header set; try the next
                        continue

                    content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
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
                        return current_url, content_type or None, None

                    if resp.status_code >= 400:
                        raise ValueError(f"HTTP {resp.status_code} fetching {current_url}")
                    body = bytearray(head)
                    async for chunk in chunks:
                        body.extend(chunk)
                        if len(body) > _size_cap_bytes():
                            raise ValueError(
                                f"PDF is larger than the {settings.CRAWL4AI_MAX_CONTENT_SIZE_MB}MB limit"
                            )
                    return current_url, "application/pdf", bytes(body)
            except ValueError:
                raise
            except httpx.HTTPError as e:
                logger.info("Resolve step could not fetch %s (%s); leaving it to the browser", current_url, e)
                return current_url, None, None
        raise ValueError(f"Too many redirects while fetching {url}")


# ── Step 4: headless browser crawl ───────────────────────────────────────────

async def _crawl_with_browser(url: str, timeout: int) -> _PageContent:
    """Render with Crawl4AI and return the page's prose, title and image
    references. Raises on any failure so the caller can fall back."""
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

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
    browser_config = BrowserConfig(headless=True, verbose=False)

    async with AsyncWebCrawler(config=browser_config) as crawler:
        # page_timeout only bounds navigation; post-processing can still
        # drag on a huge page. Hard ceiling at 2x so a scrape can't hang the
        # indexing task indefinitely.
        result = await asyncio.wait_for(crawler.arun(url, config=run_config), timeout=timeout * 2)

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
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
        for _ in range(_MAX_REDIRECT_HOPS + 1 + len(_HEADER_PROFILES)):
            await _validate_safe(current_url)
            async with client.stream("GET", current_url, headers=_HEADER_PROFILES[profile]) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise ValueError("redirect without a Location header")
                    current_url = str(httpx.URL(current_url).join(location))
                    continue
                if resp.status_code in _RETRY_STATUSES and profile + 1 < len(_HEADER_PROFILES):
                    profile += 1
                    continue
                resp.raise_for_status()
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
    return _PageContent(text=text, title=title, images=images)


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
    await _validate_safe(url)
    cap = int(settings.SCRAPE_OCR_MAX_IMAGE_MB * 1024 * 1024)
    headers = dict(_HEADER_PROFILES[0])
    headers["Accept"] = "image/*,*/*;q=0.5"
    headers["Referer"] = page_url
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout, headers=headers) as client:
        current = url
        for _ in range(_MAX_REDIRECT_HOPS + 1):
            await _validate_safe(current)
            async with client.stream("GET", current) as resp:
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
_RE_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
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
    """Reduce Crawl4AI markdown to the words a reader sees. Link *text* is
    kept, link *targets*, tooltips, images, heading markers, emphasis
    markers, table rules and bare URLs are dropped."""
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
    text = _RE_HEADING.sub("", text)
    text = _RE_BLOCKQUOTE.sub("", text)
    text = _RE_BULLET.sub("", text)
    text = _RE_STRONG.sub(r"\1", text)
    text = _RE_STRONG_US.sub(r"\1", text)
    text = _RE_EMPH.sub(r"\1", text)
    text = _RE_EMPH_US.sub(r"\1", text)
    text = _RE_FOOTNOTE.sub("", text)
    text = text.replace("|", " ")
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


# ── Step 6: PDF generation ───────────────────────────────────────────────────

_MAX_PARAGRAPH_CHARS = 3000  # ReportLab lays out one Paragraph at a time; keep them bounded


def _text_to_pdf_blocking(content: str, title: str, url: str) -> bytes:
    """
    Synchronous PDF generation (run via asyncio.to_thread).

    ReportLab's Paragraph parses its input as a small XML-ish markup, so
    every piece of untrusted text is html-escaped here, centrally. Callers
    must NOT pre-escape or the output is double-escaped ("&amp;amp;").
    """
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    pdf_buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        pdf_buffer, pagesize=letter,
        rightMargin=72, leftMargin=72, topMargin=72, bottomMargin=18,
    )
    styles = getSampleStyleSheet()
    story = []

    try:
        story.append(Paragraph(f"<b>{html_lib.escape(title)}</b>", styles["Heading1"]))
        story.append(Spacer(1, 0.3 * inch))
    except Exception as e:
        logger.warning("Failed to add title paragraph: %s", e)

    try:
        story.append(Paragraph(f"<i>Source: {html_lib.escape(url)}</i>", styles["Normal"]))
        story.append(Spacer(1, 0.2 * inch))
    except Exception as e:
        logger.warning("Failed to add source-url paragraph: %s", e)

    for para in content.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        for piece in _split_long(para, _MAX_PARAGRAPH_CHARS):
            try:
                # Single newlines inside a paragraph become line breaks so
                # list-like content keeps one item per line in the PDF.
                story.append(Paragraph(html_lib.escape(piece).replace("\n", "<br/>"), styles["Normal"]))
                story.append(Spacer(1, 0.1 * inch))
            except Exception as e:
                logger.warning("Failed to add paragraph: %s", e)

    doc.build(story)
    return pdf_buffer.getvalue()


def _split_long(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    pieces, current = [], ""
    for line in text.split("\n"):
        if current and len(current) + len(line) + 1 > limit:
            pieces.append(current)
            current = ""
        while len(line) > limit:  # a single enormous line
            pieces.append(line[:limit])
            line = line[limit:]
        current = f"{current}\n{line}" if current else line
    if current:
        pieces.append(current)
    return pieces
