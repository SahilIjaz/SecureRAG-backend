import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel

from app.models.document import DocumentSource, DocumentStatus


# ---------------------------------------------------------------------------
# Community sample document response
# Documents uploaded by other users in the same business category
# ---------------------------------------------------------------------------

class CommunityDocumentResponse(BaseModel):
    id: uuid.UUID                  # Document.id — pass this to /select-sample
    original_filename: str
    file_size_mb: float
    mime_type: str
    file_url: Optional[str]
    business_category: str         # from the uploader's tenant

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
    source_url: Optional[str]   # For scraped documents: original URL
    file_url: Optional[str]     # Cloudinary secure URL
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
    document_id: uuid.UUID   # ID of a community-uploaded Document to copy into your workspace


# ---------------------------------------------------------------------------
# Web scraping request
# ---------------------------------------------------------------------------

class ScrapWebsiteRequest(BaseModel):
    urls: List[str]   # URLs to scrape (must start with http:// or https://)


# ---------------------------------------------------------------------------
# Grouped response for upload/select endpoints
# ---------------------------------------------------------------------------

class DocumentsResponse(BaseModel):
    message: str
    documents: List[DocumentResponse]
    total_count: int
    total_storage_mb: float
