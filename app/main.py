import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from sqlalchemy import text

from app.api.frontend.router import router as frontend_router
from app.api.v1.router import router as v1_router
from app.config import settings
from app.core.rate_limit import limiter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

async def ensure_frontend_schema() -> None:
    """
    Idempotently apply the frontend-compat schema additions
    (mirrors migrations/add_frontend_compat.sql): new tables via
    metadata.create_all + ADD COLUMN IF NOT EXISTS for existing tables.
    """
    import app.models  # noqa: F401 — register all models on Base.metadata
    from app.database import Base, async_engine

    # Must run in its own transaction, committed before anything else below
    # (including create_all) touches the `documentsource` enum — Postgres
    # will not let a newly-added enum value be read or written until the
    # ALTER TYPE that added it has actually committed. Keep this separate
    # from `alter_statements`, which all run inside one shared transaction;
    # bundling this in there too would make the enum value's availability
    # depend on that whole batch committing first, which is exactly the
    # fragile-by-accident ordering this split avoids.
    #
    # Only applies to an *existing* database being upgraded — `ALTER TYPE`
    # requires the type to already exist. On a genuinely fresh database
    # (nothing created yet), skip it entirely: create_all() below creates
    # `documentsource` from the Python enum, which already includes 'faq'
    # as a member, so there's nothing to alter.
    async with async_engine.begin() as conn:
        type_exists = await conn.scalar(
            text("SELECT 1 FROM pg_type WHERE typname = 'documentsource'")
        )
        if type_exists:
            await conn.execute(text("ALTER TYPE documentsource ADD VALUE IF NOT EXISTS 'faq'"))

    alter_statements = [
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS chunk_count INTEGER",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS question TEXT",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS answer TEXT",
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64)",
        "CREATE INDEX IF NOT EXISTS ix_documents_content_hash ON documents (content_hash)",
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS has_documents BOOLEAN",
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS onboarding_completed_at TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT",
        # The dashboard sends free-form team sizes ("Just me", "2–10", ...) and
        # business categories ("SaaS", ...) that predate-constraint sets reject.
        "ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_employee_count_range_check",
        "ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_business_category_check",
    ]
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for stmt in alter_statements:
            await conn.execute(text(stmt))

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await ensure_frontend_schema()
        logger.info("Frontend-compat schema is up to date")
    except Exception as e:
        logger.error("Failed to apply frontend-compat schema: %s", e)

    import asyncio

    from app.services.indexing_service import stale_document_sweep_loop
    asyncio.create_task(stale_document_sweep_loop())

    logger.info(
        "%s API is running (debug=%s)",
        settings.APP_NAME,
        settings.DEBUG,
    )
    yield

app = FastAPI(
    title="SecureRAG++ API",
    version="1.0.0",
    description=(
        "Multi-tenant Retrieval-Augmented Generation platform. "
        "Provides secure document ingestion, semantic search, and LLM-powered Q&A "
        "with per-tenant isolation, quota enforcement, and subscription management."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.state.limiter = limiter

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request, exc):
    return JSONResponse(
        status_code=429,
        content={"detail": "Rate limit exceeded. Please try again later."}
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173", "http://localhost:5174", "http://localhost:5175", "http://localhost:3000", ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=3600, )

app.include_router(v1_router, prefix="/api/v1")

# Frontend-compat API — paths/shapes match Nexus-frontend/src/api/*.api.ts.
app.include_router(frontend_router, prefix="/api")

@app.get("/health", tags=["health"], summary="Health check")
async def health_check() -> dict:
    """Return application health status."""
    return {"status": "ok", "app": settings.APP_NAME, "version": "1.0.0"}
