"""
Whole-site discovery (app/core/scraper.discover_site_urls and friends) and
the weekly refresh sweep (app/services/indexing_service.
scrape_refresh_sweep_loop / refresh_due_scraped_documents).

Discovery/probe tests use httpx.MockTransport (no real sockets), same
pattern as tests/test_scraper.py. The refresh-sweep tests are DB-backed:
a throwaway tenant + scraped Document on the configured database, hard-
deleted at the end. Skips itself when the database is unreachable.

Run with:  PYTHONPATH=. ./venv/bin/pytest tests/test_site_crawl.py -v
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.core import scraper
from app.models.document import Document, DocumentSource, DocumentStatus
from app.models.tenant import Tenant


def _client_factory(monkeypatch, handler, resolve_map: dict[str, str]):
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
    monkeypatch.setattr(scraper.settings, "SCRAPE_ROBOTS_TXT_ENABLED", False)  # isolate discovery from robots fetches
    monkeypatch.setattr(scraper.settings, "SCRAPE_MIN_REQUEST_INTERVAL_SECONDS", 0)  # don't slow the test suite down
    scraper._robots_cache.clear()
    scraper._last_request_at.clear()


# ── Sitemap discovery ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sitemap_urls_are_discovered_and_filtered_to_same_site(monkeypatch):
    sitemap = (
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://site.example/a</loc></url>"
        "<url><loc>https://site.example/b</loc></url>"
        "<url><loc>https://other-domain.example/c</loc></url>"
        "</urlset>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, headers={"content-type": "application/xml"}, text=sitemap)
        return httpx.Response(404)

    _client_factory(monkeypatch, handler, {"site.example": "1.2.3.4"})
    urls = await scraper._urls_from_sitemap("https://site.example/sitemap.xml", "site.example")
    assert set(urls) == {"https://site.example/a", "https://site.example/b"}


@pytest.mark.asyncio
async def test_sitemap_index_is_followed_one_level(monkeypatch):
    index = (
        '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<sitemap><loc>https://site.example/sitemap-pages.xml</loc></sitemap>"
        "</sitemapindex>"
    )
    child = (
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://site.example/deep-page</loc></url></urlset>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, headers={"content-type": "application/xml"}, text=index)
        if request.url.path == "/sitemap-pages.xml":
            return httpx.Response(200, headers={"content-type": "application/xml"}, text=child)
        return httpx.Response(404)

    _client_factory(monkeypatch, handler, {"site.example": "1.2.3.4"})
    urls = await scraper._urls_from_sitemap("https://site.example/sitemap.xml", "site.example")
    assert urls == ["https://site.example/deep-page"]


@pytest.mark.asyncio
async def test_discover_site_urls_falls_back_to_bfs_when_no_sitemap(monkeypatch):
    home = '<html><body><a href="/about">About</a><a href="/pricing">Pricing</a></body></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        if "sitemap" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html"}, text=home)

    _client_factory(monkeypatch, handler, {"nositemap.example": "1.2.3.4"})
    urls = await scraper.discover_site_urls("https://nositemap.example/", max_pages=5, max_depth=1)
    assert "https://nositemap.example/" in urls
    assert any(u.endswith("/about") for u in urls)
    assert any(u.endswith("/pricing") for u in urls)


@pytest.mark.asyncio
async def test_discover_site_urls_respects_max_pages_cap(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if "sitemap" in request.url.path:
            return httpx.Response(404)
        links = "".join(f'<a href="/page{i}">p{i}</a>' for i in range(20))
        return httpx.Response(200, headers={"content-type": "text/html"}, text=f"<html><body>{links}</body></html>")

    _client_factory(monkeypatch, handler, {"bignsite.example": "1.2.3.4"})
    urls = await scraper.discover_site_urls("https://bignsite.example/", max_pages=4, max_depth=1)
    assert len(urls) <= 4


@pytest.mark.asyncio
async def test_bfs_ignores_cross_domain_links(monkeypatch):
    home = '<html><body><a href="https://elsewhere.example/x">off-site</a><a href="/local">on-site</a></body></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        if "sitemap" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html"}, text=home)

    _client_factory(monkeypatch, handler, {"onesite.example": "1.2.3.4", "elsewhere.example": "5.6.7.8"})
    urls = await scraper.discover_site_urls("https://onesite.example/", max_pages=5, max_depth=1)
    assert all("elsewhere.example" not in u for u in urls)


# ── Conditional refresh probe ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_for_update_reports_not_modified_on_304(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("if-none-match") == 'W/"abc"'
        return httpx.Response(304)

    _client_factory(monkeypatch, handler, {"stable.example": "1.2.3.4"})
    result = await scraper.check_for_update("https://stable.example/", etag='W/"abc"', last_modified=None)
    assert result == {"not_modified": True, "etag": 'W/"abc"', "last_modified": None, "content_hash": None}


@pytest.mark.asyncio
async def test_check_for_update_reports_hash_on_200(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"etag": '"new"'}, content=b"new body content")

    _client_factory(monkeypatch, handler, {"changed.example": "1.2.3.4"})
    result = await scraper.check_for_update("https://changed.example/", etag=None, last_modified=None)
    assert result["not_modified"] is False
    assert result["etag"] == '"new"'
    assert result["content_hash"] is not None


@pytest.mark.asyncio
async def test_check_for_update_returns_none_on_failure(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    _client_factory(monkeypatch, handler, {"broken.example": "1.2.3.4"})
    assert await scraper.check_for_update("https://broken.example/", None, None) is None


@pytest.mark.asyncio
async def test_check_for_update_returns_none_when_robots_disallows(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"shouldn't be reached")

    async def _always_disallowed(url):
        return False

    _client_factory(monkeypatch, handler, {"disallowed.example": "1.2.3.4"})
    monkeypatch.setattr(scraper, "_robots_allowed", _always_disallowed)
    assert await scraper.check_for_update("https://disallowed.example/", None, None) is None


# ── Refresh sweep (DB-backed) ─────────────────────────────────────────────────

async def _fresh_engine():
    from app.database import async_engine
    await async_engine.dispose()


async def _make_tenant() -> uuid.UUID:
    from app.database import AsyncSessionLocal

    await _fresh_engine()
    tenant_id = uuid.uuid4()
    try:
        async with AsyncSessionLocal() as db:
            db.add(Tenant(id=tenant_id, workspace_name=f"qa-crawl-{tenant_id.hex[:6]}", slug=f"qa-crawl-{tenant_id.hex[:10]}"))
            await db.commit()
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"database unavailable: {e}")
    return tenant_id


async def _drop_tenant(tenant_id: uuid.UUID) -> None:
    from app.database import AsyncSessionLocal
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        await db.execute(text("delete from tenants where id = :id"), {"id": tenant_id})
        await db.commit()


async def _make_ready_scraped_doc(tenant_id: uuid.UUID, *, url: str, content_hash: str, days_old: int) -> uuid.UUID:
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        doc = Document(
            tenant_id=tenant_id, original_filename=url, file_size_mb=0.1, mime_type="text/markdown",
            source=DocumentSource.scraped, source_url=url, status=DocumentStatus.ready,
            content_hash=content_hash, etag=None, last_modified_header=None,
            last_checked_at=datetime.now(timezone.utc) - timedelta(days=days_old),
        )
        db.add(doc)
        await db.commit()
        await db.refresh(doc)
        return doc.id


@pytest.mark.asyncio
async def test_refresh_sweep_skips_documents_not_yet_due(monkeypatch):
    # The sweep is intentionally global (every tenant's due documents), so
    # this shared dev database may have other real due documents from
    # elsewhere — assert our own fresh one specifically was never probed,
    # not that the sweep probed nothing at all.
    from app.services import indexing_service

    tenant_id = await _make_tenant()
    try:
        fresh_url = f"https://fresh-{tenant_id.hex[:8]}.example/"
        await _make_ready_scraped_doc(tenant_id, url=fresh_url, content_hash="h1", days_old=1)
        probed_urls: list[str] = []

        async def fake_probe(url, etag, last_modified):
            probed_urls.append(url)
            return {"not_modified": True, "etag": None, "last_modified": None, "content_hash": None}

        monkeypatch.setattr("app.core.scraper.check_for_update", fake_probe)
        await indexing_service.refresh_due_scraped_documents()
        assert fresh_url not in probed_urls
    finally:
        await _drop_tenant(tenant_id)


@pytest.mark.asyncio
async def test_refresh_sweep_updates_last_checked_at_when_unchanged(monkeypatch):
    from sqlalchemy import select as sa_select

    from app.database import AsyncSessionLocal
    from app.services import indexing_service

    tenant_id = await _make_tenant()
    try:
        doc_id = await _make_ready_scraped_doc(tenant_id, url="https://unchanged.example/", content_hash="samehash", days_old=10)

        async def fake_probe(url, etag, last_modified):
            return {"not_modified": False, "etag": "e2", "last_modified": None, "content_hash": "samehash"}

        monkeypatch.setattr("app.core.scraper.check_for_update", fake_probe)
        rescheduled = {"ids": None}
        monkeypatch.setattr(
            indexing_service, "schedule_url_scrape", lambda ids: rescheduled.__setitem__("ids", ids)
        )

        await indexing_service.refresh_due_scraped_documents()

        async with AsyncSessionLocal() as db:
            row = (await db.execute(sa_select(Document).where(Document.id == doc_id))).scalar_one()
            assert row.etag == "e2"
            assert row.last_checked_at > datetime.now(timezone.utc) - timedelta(minutes=1)
        # Other real due documents sharing this dev database may have a
        # different stored hash than our fake probe's fixed "samehash" and
        # so get (correctly) treated as changed — only assert ours wasn't.
        assert doc_id not in (rescheduled["ids"] or [])
    finally:
        await _drop_tenant(tenant_id)


@pytest.mark.asyncio
async def test_refresh_sweep_reschedules_a_changed_document(monkeypatch):
    from app.services import indexing_service

    tenant_id = await _make_tenant()
    try:
        doc_id = await _make_ready_scraped_doc(tenant_id, url="https://changed.example/", content_hash="oldhash", days_old=10)

        async def fake_probe(url, etag, last_modified):
            return {"not_modified": False, "etag": "e-new", "last_modified": None, "content_hash": "newhash"}

        monkeypatch.setattr("app.core.scraper.check_for_update", fake_probe)
        rescheduled = {"ids": None}
        monkeypatch.setattr(
            indexing_service, "schedule_url_scrape", lambda ids: rescheduled.__setitem__("ids", ids)
        )

        await indexing_service.refresh_due_scraped_documents()
        # The sweep is global — don't assert it rescheduled *only* ours,
        # just that ours was definitely among what it rescheduled.
        assert rescheduled["ids"] is not None and doc_id in rescheduled["ids"]
    finally:
        await _drop_tenant(tenant_id)


@pytest.mark.asyncio
async def test_refresh_sweep_leaves_document_alone_when_probe_fails(monkeypatch):
    from sqlalchemy import select as sa_select

    from app.database import AsyncSessionLocal
    from app.services import indexing_service

    tenant_id = await _make_tenant()
    try:
        doc_id = await _make_ready_scraped_doc(tenant_id, url="https://flaky.example/", content_hash="h1", days_old=10)

        async def fake_probe(url, etag, last_modified):
            return None

        monkeypatch.setattr("app.core.scraper.check_for_update", fake_probe)
        rescheduled = {"ids": None}
        monkeypatch.setattr(
            indexing_service, "schedule_url_scrape", lambda ids: rescheduled.__setitem__("ids", ids)
        )

        await indexing_service.refresh_due_scraped_documents()

        async with AsyncSessionLocal() as db:
            row = (await db.execute(sa_select(Document).where(Document.id == doc_id))).scalar_one()
            assert row.status == DocumentStatus.ready
            assert row.content_hash == "h1"  # untouched
        assert rescheduled["ids"] is None
    finally:
        await _drop_tenant(tenant_id)


@pytest.mark.asyncio
async def test_refresh_sweep_disabled_does_nothing(monkeypatch):
    from app.services import indexing_service

    monkeypatch.setattr(indexing_service.settings, "SCRAPE_REFRESH_ENABLED", False)
    calls = {"n": 0}

    async def fake_probe(url, etag, last_modified):
        calls["n"] += 1
        return None

    monkeypatch.setattr("app.core.scraper.check_for_update", fake_probe)
    await indexing_service.refresh_due_scraped_documents()
    assert calls["n"] == 0
