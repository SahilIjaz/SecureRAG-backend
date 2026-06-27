import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel

from app.models.document import DocumentSource, DocumentStatus

class CommunityDocumentResponse(BaseModel):
    id: uuid.UUID original_filename: str
    file_size_mb: float
    mime_type: str
    file_url: Optional[str]
    business_category: str
    model_config = {"from_attributes": True}

class DocumentResponse(BaseModel):
    id: uuid.UUID
    original_filename: str
    file_size_mb: float
    mime_type: str
    source: DocumentSource
    status: DocumentStatus
    sample_document_id: Optional[uuid.UUID]
    source_url: Optional[str] file_url: Optional[str] created_at: datetime

    model_config = {"from_attributes": True}

class DocumentPreferenceRequest(BaseModel):
    has_documents: bool

class SelectSampleDocumentRequest(BaseModel):
    document_id: uuid.UUID

class ScrapWebsiteRequest(BaseModel):
    urls: List[str]

class DocumentsResponse(BaseModel):
    message: str
    documents: List[DocumentResponse]
    total_count: int
    total_storage_mb: float
