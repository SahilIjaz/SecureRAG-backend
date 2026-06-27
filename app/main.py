import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.api.v1.router import router as v1_router
from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
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

limiter = Limiter(key_func=get_remote_address)
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
        "http://localhost:5173",             "http://localhost:5174",             "http://localhost:5175",             "http://localhost:3000",         ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=3600,  )

app.include_router(v1_router, prefix="/api/v1")

@app.get("/health", tags=["health"], summary="Health check")
async def health_check() -> dict:
    """Return application health status."""
    return {"status": "ok", "app": settings.APP_NAME, "version": "1.0.0"}
