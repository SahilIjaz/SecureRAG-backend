import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import router as v1_router
from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

app = FastAPI(
    title="SecureRAG++ API",
    version="1.0.0",
    description=(
        "Multi-tenant Retrieval-Augmented Generation platform. "
        "Provides secure document ingestion, semantic search, and LLM-powered Q&A "
        "with per-tenant isolation, quota enforcement, and subscription management."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict to specific origins in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(v1_router, prefix="/api/v1")

# ---------------------------------------------------------------------------
# Lifecycle events
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def on_startup() -> None:
    logger.info(
        "%s API is running (debug=%s)",
        settings.APP_NAME,
        settings.DEBUG,
    )


# ---------------------------------------------------------------------------
# Root endpoints
# ---------------------------------------------------------------------------


@app.get("/health", tags=["health"], summary="Health check")
async def health_check() -> dict:
    """Return application health status."""
    return {"status": "ok", "app": settings.APP_NAME}
