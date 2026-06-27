import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr


class UserResponse(BaseModel):
    id: uuid.UUID
    full_name: str
    email: EmailStr
    is_email_verified: bool
    is_active: bool
    created_at: datetime
    tenant_id: uuid.UUID

    model_config = {"from_attributes": True}


class UserWithTenantResponse(UserResponse):
    workspace_name: Optional[str] = None
    slug: Optional[str] = None

