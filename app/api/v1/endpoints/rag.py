"""RAG (Retrieval-Augmented Generation) endpoints for Q&A over documents."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.database import get_db
from app.services.rag_service import retrieve_context_for_query, answer_question
from app.services.auth_service import get_any_valid_user

router = APIRouter(prefix="/rag", tags=["RAG"])
limiter = Limiter(key_func=get_remote_address)

class QueryRequest(BaseModel):
    """Query request for RAG."""
    query: str

class QueryResponse(BaseModel):
    """RAG query response."""
    answer: str
    sources: list[str]
    model: str

@router.post(
    "/query",
    response_model=QueryResponse,
    status_code=status.HTTP_200_OK,
    summary="Ask a question about documents",
)
@limiter.limit("20/minute")
async def query_documents(
    request: Request,
    body: QueryRequest,
    user=Depends(get_any_valid_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Ask a question about indexed documents.

    This endpoint:
    1. Converts query to embedding
    2. Searches Pinecone for relevant chunks
    3. Sends chunks + query to Claude for answer generation
    4. Returns answer with source chunks

    Uses RAG (Retrieval-Augmented Generation) to ground answers in your documents.
    """
    try:
        if not body.query or len(body.query.strip()) < 3:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Query must be at least 3 characters",
            )

        result = await answer_question(
            tenant_id=str(user.tenant_id),
            query=body.query,
        )

        return QueryResponse(
            answer=result["answer"],
            sources=result["sources"],
            model=result["model"],
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to answer question: {str(e)}",
        )

@router.post(
    "/health",
    status_code=status.HTTP_200_OK,
    summary="Check RAG system health",
)
@limiter.limit("10/minute")
async def check_rag_health(
    request: Request,
    user=Depends(get_any_valid_user),
):
    """Check if Pinecone vector store is accessible."""
    try:
        from app.core.vector_store import get_index

        index = get_index()
        stats = index.describe_index_stats()

        return {
            "status": "healthy",
            "vector_store": "pinecone",
            "dimension": stats.get("dimension", "unknown"),
            "index_fullness": stats.get("index_fullness", "unknown"),
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"RAG system unavailable: {str(e)}",
        )
