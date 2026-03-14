import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr

from app.schemas.tenant import TenantResponse


class UserResponse(BaseModel):
    id: uuid.UUID
    full_name: str
    email: EmailStr
    is_email_verified: bool
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class UserWithTenantResponse(UserResponse):
    tenant_id: uuid.UUID
    tenant: Optional[TenantResponse] = None

    model_config = {"from_attributes": True}
