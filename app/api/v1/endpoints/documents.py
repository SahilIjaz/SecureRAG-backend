from typing import List

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.document import (
    DocumentResponse,
    DocumentsResponse,
    SampleDocumentResponse,
    SelectSampleDocumentRequest,
)
from app.services import auth_service, document_service

router = APIRouter(prefix="/documents", tags=["documents"])


@router.get(
    "/samples",
    response_model=List[SampleDocumentResponse],
    summary="List sample documents for user's business category",
)
async def list_sample_documents(
    user=Depends(auth_service.get_onboarding_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns sample documents filtered by the tenant's business category.
    Requires onboarding token (issued after OTP verification).
    """
    docs = await document_service.get_sample_documents(user, db)
    return docs


@router.post(
    "/upload",
    response_model=DocumentsResponse,
    status_code=201,
    summary="Upload one or more documents (PDF, DOCX, TXT)",
)
async def upload_documents(
    files: List[UploadFile] = File(...),
    user=Depends(auth_service.get_onboarding_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Upload user documents. Validates file types/sizes against tenant quota.
    Requires onboarding token.
    """
    saved = await document_service.upload_documents(user, files, db)
    total_storage = round(sum(d.file_size_mb for d in saved), 4)
    return DocumentsResponse(
        message=f"{len(saved)} document(s) uploaded successfully.",
        documents=[DocumentResponse.model_validate(d) for d in saved],
        total_count=len(saved),
        total_storage_mb=total_storage,
    )


@router.post(
    "/select-sample",
    response_model=DocumentResponse,
    status_code=201,
    summary="Select a sample document for the tenant",
)
async def select_sample_document(
    body: SelectSampleDocumentRequest,
    user=Depends(auth_service.get_onboarding_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Creates a Document record (source=sample) linking to the chosen SampleDocument.
    Requires onboarding token.
    """
    doc = await document_service.select_sample_document(user, body.sample_document_id, db)
    return DocumentResponse.model_validate(doc)


@router.get(
    "/",
    response_model=DocumentsResponse,
    summary="List all documents for the authenticated tenant",
)
async def list_documents(
    user=Depends(auth_service.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns all active documents for the current tenant.
    Requires access token.
    """
    docs = await document_service.get_tenant_documents(user, db)
    total_storage = round(sum(d.file_size_mb for d in docs), 4)
    return DocumentsResponse(
        message="Documents retrieved successfully.",
        documents=[DocumentResponse.model_validate(d) for d in docs],
        total_count=len(docs),
        total_storage_mb=total_storage,
    )
