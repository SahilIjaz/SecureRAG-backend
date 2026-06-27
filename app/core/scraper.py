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

            markdown_content = result.markdown or ""
            page_title = result.metadata.get("title", "Untitled") if result.metadata else "Untitled"

            if not markdown_content.strip():
                raise ValueError(f"No content extracted from {url}")

            logger.info(f"Scraped {url} - Title: {page_title}")

            pdf_bytes = await _markdown_to_pdf(markdown_content, page_title, url)

            return pdf_bytes, page_title

    except Exception as e:
        logger.error(f"Scraping failed for {url}: {str(e)}")
        raise ValueError(f"Failed to scrape {url}: {str(e)}")

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
    """
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

    title_para = Paragraph(f"<b>{title}</b>", styles["Heading1"])
    story.append(title_para)
    story.append(Spacer(1, 0.3 * inch))

    url_para = Paragraph(f"<i>Source: {url}</i>", styles["Normal"])
    story.append(url_para)
    story.append(Spacer(1, 0.2 * inch))

    paragraphs = content.split("\n\n")
    for para in paragraphs:
        if para.strip():
            try:
                p = Paragraph(para.strip(), styles["Normal"])
                story.append(p)
                story.append(Spacer(1, 0.1 * inch))
            except Exception as e:
                logger.warning(f"Failed to add paragraph: {e}")
                continue

    doc.build(story)
    pdf_bytes = pdf_buffer.getvalue()
    pdf_buffer.close()

    return pdf_bytes
