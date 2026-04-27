import uuid
from typing import List

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.document import (
    CommunityDocumentResponse,
    DocumentResponse,
    DocumentsResponse,
    SelectSampleDocumentRequest,
)
from app.services import auth_service, document_service
from app.services.auth_service import get_any_valid_user


class SelectPlatformSampleDocumentsRequest(BaseModel):
    sample_document_ids: List[uuid.UUID]

router = APIRouter(prefix="/documents", tags=["documents"])


@router.get(
    "/samples",
    response_model=List[CommunityDocumentResponse],
    summary="List community documents for user's business category",
)
async def list_sample_documents(
    user=Depends(get_any_valid_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns documents uploaded by other users in the same business category.
    These act as community sample datasets. Requires onboarding token.
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
    user=Depends(get_any_valid_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Upload user documents to Cloudinary. Validates file types/sizes against quota.
    Uploaded files become available as community samples for others in the same category.
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
    summary="Add a community document to your workspace",
)
async def select_sample_document(
    body: SelectSampleDocumentRequest,
    user=Depends(get_any_valid_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Copies a community-uploaded document into the current user's workspace.
    Pass the document_id from GET /documents/samples.
    Requires onboarding token.
    """
    doc = await document_service.select_sample_document(user, body.document_id, db)
    return DocumentResponse.model_validate(doc)


@router.post(
    "/select-platform-samples",
    response_model=DocumentsResponse,
    status_code=201,
    summary="Add platform sample documents to your workspace",
)
async def select_platform_sample_documents(
    body: SelectPlatformSampleDocumentsRequest,
    user=Depends(get_any_valid_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Copies platform sample documents into the current user's workspace.
    Pass the sample_document_ids from GET /auth/sample-documents.
    Requires onboarding token.
    """
    docs = await document_service.select_platform_sample_documents(
        user, body.sample_document_ids, db
    )
    total_storage = round(sum(d.file_size_mb for d in docs), 4)
    return DocumentsResponse(
        message=f"{len(docs)} sample document(s) added successfully.",
        documents=[DocumentResponse.model_validate(d) for d in docs],
        total_count=len(docs),
        total_storage_mb=total_storage,
    )


@router.get(
    "/",
    response_model=DocumentsResponse,
    summary="List all documents in your workspace",
)
async def list_documents(
    user=Depends(get_any_valid_user),
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
