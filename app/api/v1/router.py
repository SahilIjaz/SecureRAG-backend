from fastapi import APIRouter

from app.api.v1.endpoints import auth, documents, rag

router = APIRouter()

# Auth
router.include_router(auth.router)

# Documents
router.include_router(documents.router)

# RAG
router.include_router(rag.router)


@router.get("/ping", tags=["health"], summary="Liveness check")
async def ping() -> dict:
    """Return a simple pong response to confirm the API v1 router is reachable."""
    return {"message": "pong"}
