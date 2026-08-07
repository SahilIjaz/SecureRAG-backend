"""
Web scraping service using Crawl4AI.
Crawls websites and converts content to PDF for storage on Cloudinary.
Includes SSRF protection via IP validation.
"""

import asyncio
import io
import logging
from typing import Tuple

from crawl4ai import AsyncWebCrawler
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
from reportlab.lib.units import inch

from app.core.ip_validator import validate_url_safe

logger = logging.getLogger(__name__)

async def scrape_website_to_pdf(url: str, timeout: int = 30) -> Tuple[bytes, str]:
    """
    Scrapes a website using Crawl4AI and converts content to PDF.

    Args:
        url: Website URL to scrape
        timeout: Request timeout in seconds

    Returns:
        (pdf_bytes, page_title)

    Raises:
        ValueError: If URL is invalid or scraping fails
    """
    if not url.startswith(("http://", "https://")):
        raise ValueError("URL must start with http:// or https://")

    validate_url_safe(url)

    try:
        async with AsyncWebCrawler(timeout=timeout) as crawler:
            result = await crawler.arun(url)

            if not result.success:
                raise ValueError(f"Failed to crawl {url}: {result.error_message}")

            # Best-effort defense-in-depth against SSRF via DNS-rebinding:
            # validate_url_safe() above ran before the crawl started, but
            # Crawl4AI does its own independent DNS resolution and may have
            # followed redirects — re-check whatever it actually landed on.
            # This doesn't fully close the TOCTOU window (Crawl4AI's browser
            # networking isn't interceptable for true IP-pinning without
            # forking it), but it catches the common case where the final
            # URL differs from the one we validated.
            final_url = getattr(result, "url", None)
            if final_url and final_url != url:
                validate_url_safe(final_url)

            markdown_content = result.markdown or ""
            page_title = result.metadata.get("title", "Untitled") if result.metadata else "Untitled"

            if not markdown_content.strip():
                raise ValueError(f"No content extracted from {url}")

            logger.info(f"Scraped {url} - Title: {page_title}")

            pdf_bytes = await _markdown_to_pdf(markdown_content, page_title, url)

            return pdf_bytes, page_title

    except Exception as e:
        # Crawl4AI's extraction pipeline crashes on some sites (bot-protected
        # or nonstandard HTML with no parseable body). Fall back to a plain
        # HTTP fetch + BeautifulSoup text extraction before giving up.
        logger.warning(f"Crawl4AI failed for {url} ({e}); trying plain-HTTP fallback")
        try:
            return await _scrape_fallback(url, timeout)
        except Exception as fallback_error:
            logger.error(f"Scraping failed for {url}: {str(fallback_error)}")
            raise ValueError(f"Failed to scrape {url}: {str(fallback_error)}")

_MAX_REDIRECT_HOPS = 5

async def _scrape_fallback(url: str, timeout: int) -> Tuple[bytes, str]:
    """Plain httpx GET + BeautifulSoup text extraction, converted to PDF."""
    import httpx
    from bs4 import BeautifulSoup

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    # Redirects are followed manually (not via httpx's follow_redirects=True)
    # so every hop gets SSRF-validated *before* the request fires — the
    # original single post-fetch check only validated the final landing URL,
    # after the request (including any redirect chain) had already gone out.
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout, headers=headers) as client:
        current_url = url
        resp = None
        for _ in range(_MAX_REDIRECT_HOPS + 1):
            validate_url_safe(current_url)
            resp = await client.get(current_url)
            if not resp.is_redirect:
                break
            location = resp.headers.get("location")
            if not location:
                break
            current_url = str(httpx.URL(current_url).join(location))
        else:
            raise ValueError(f"Too many redirects while fetching {url}")
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()

    page_title = (soup.title.string or "").strip() if soup.title and soup.title.string else url
    text = soup.get_text(separator="\n")
    paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
    # Escaping for PDF/ReportLab markup happens centrally in
    # _markdown_to_pdf_blocking — don't pre-escape here too, or fallback
    # content would get double-escaped ("&" -> "&amp;" -> "&amp;amp;").
    content = "\n\n".join(paragraphs)

    if not content.strip():
        raise ValueError(f"No content extracted from {url}")

    logger.info(f"Scraped {url} via fallback - Title: {page_title}")
    pdf_bytes = await _markdown_to_pdf(content, page_title, url)
    return pdf_bytes, page_title

async def _markdown_to_pdf(content: str, title: str, url: str) -> bytes:
    """
    Converts markdown content to PDF using ReportLab.
    Runs in executor to avoid blocking.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _markdown_to_pdf_blocking, content, title, url)

def _markdown_to_pdf_blocking(content: str, title: str, url: str) -> bytes:
    """
    Synchronous PDF generation.

    ReportLab's Paragraph treats its input as a small XML/HTML markup
    language, so any raw "&", "<", ">" in scraped content (a URL query
    string is the most common real-world trigger) can break — or, if
    unlucky, misrender as — markup rather than literal text. Every piece of
    untrusted text handed to Paragraph() here is escaped, centrally, in
    this one function — callers should NOT pre-escape (that would produce
    double-escaped output for callers that do).
    """
    import html as html_lib

    pdf_buffer = io.BytesIO()

    doc = SimpleDocTemplate(
        pdf_buffer,
        pagesize=letter,
        rightMargin=72,
        leftMargin=72,
        topMargin=72,
        bottomMargin=18,
    )

    styles = getSampleStyleSheet()
    story = []

    try:
        title_para = Paragraph(f"<b>{html_lib.escape(title)}</b>", styles["Heading1"])
        story.append(title_para)
        story.append(Spacer(1, 0.3 * inch))
    except Exception as e:
        logger.warning(f"Failed to add title paragraph: {e}")

    try:
        url_para = Paragraph(f"<i>Source: {html_lib.escape(url)}</i>", styles["Normal"])
        story.append(url_para)
        story.append(Spacer(1, 0.2 * inch))
    except Exception as e:
        logger.warning(f"Failed to add source-url paragraph: {e}")

    paragraphs = content.split("\n\n")
    for para in paragraphs:
        if para.strip():
            try:
                p = Paragraph(html_lib.escape(para.strip()), styles["Normal"])
                story.append(p)
                story.append(Spacer(1, 0.1 * inch))
            except Exception as e:
                logger.warning(f"Failed to add paragraph: {e}")
                continue

    doc.build(story)
    pdf_bytes = pdf_buffer.getvalue()
    pdf_buffer.close()

    return pdf_bytes
