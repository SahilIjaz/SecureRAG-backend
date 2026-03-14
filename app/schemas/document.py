import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel

from app.models.document import DocumentSource, DocumentStatus


# ---------------------------------------------------------------------------
# Sample document responses
# ---------------------------------------------------------------------------

class SampleDocumentResponse(BaseModel):
    id: uuid.UUID
    title: str
    description: str
    filename: str
    file_size_mb: float
    business_category: str

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Uploaded / selected document response
# ---------------------------------------------------------------------------

class DocumentResponse(BaseModel):
    id: uuid.UUID
    original_filename: str
    file_size_mb: float
    mime_type: str
    source: DocumentSource
    status: DocumentStatus
    sample_document_id: Optional[uuid.UUID]
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Document preference request (step after workspace name)
# ---------------------------------------------------------------------------

class DocumentPreferenceRequest(BaseModel):
    has_documents: bool  # True = user will upload, False = show sample docs


# ---------------------------------------------------------------------------
# Select sample document request
# ---------------------------------------------------------------------------

class SelectSampleDocumentRequest(BaseModel):
    sample_document_id: uuid.UUID


# ---------------------------------------------------------------------------
# Grouped response for upload/select endpoints
# ---------------------------------------------------------------------------

class DocumentsResponse(BaseModel):
    message: str
    documents: List[DocumentResponse]
    total_count: int
    total_storage_mb: float
