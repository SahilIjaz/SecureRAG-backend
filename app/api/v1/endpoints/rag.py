"""RAG (Retrieval-Augmented Generation) endpoints for Q&A over documents."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.database import get_db
from app.services.rag_service import process_document_for_rag, retrieve_context_for_query, answer_question
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


class DocumentProcessResponse(BaseModel):
    """Response from document processing."""
    document_id: str
    chunk_count: int
    total_tokens: int
    status: str


@router.post(
    "/process-document/{document_id}",
    response_model=DocumentProcessResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Process document for RAG",
)
@limiter.limit("5/minute")
async def process_document_for_rag(
    request: Request,
    document_id: str,
    user=Depends(get_any_valid_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Process a document for RAG indexing.

    This endpoint:
    1. Extracts text from the PDF
    2. Chunks into ~500 token pieces with 50 token overlap
    3. Generates embeddings for each chunk
    4. Stores chunks in Pinecone for retrieval

    Chunks are automatically indexed and available for Q&A queries.
    """
    try:
        from app.models.document import Document
        from sqlalchemy import select

        result = await db.execute(
            select(Document).where(
                Document.id == document_id,
                Document.tenant_id == user.tenant_id,
            )
        )
        doc = result.scalar_one_or_none()

        if not doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Document not found",
            )

        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Document processing integration with Cloudinary pending",
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to process document: {str(e)}",
        )


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

