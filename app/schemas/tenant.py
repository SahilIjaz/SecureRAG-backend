import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


VALID_EMPLOYEE_RANGES = {"1-15", "16-49", "50-249", "250+"}


class OrganizationInfoRequest(BaseModel):
    business_category: str = Field(
        ..., min_length=1, max_length=255, examples=["Technology"]
    )
    employee_count_range: str = Field(
        ..., examples=["1-15"], description="One of: 1-15, 16-49, 50-249, 250+"
    )

    @classmethod
    def validate_employee_range(cls, v: str) -> str:
        if v not in VALID_EMPLOYEE_RANGES:
            raise ValueError(
                f"employee_count_range must be one of {sorted(VALID_EMPLOYEE_RANGES)}"
            )
        return v


class WorkspaceSetupRequest(BaseModel):
    workspace_name: str = Field(
        ..., min_length=1, max_length=255, examples=["Acme Corp Workspace"]
    )


class DocumentPreferenceRequest(BaseModel):
    use_sample_documents: bool = Field(
        ...,
        examples=[True],
        description="Whether to pre-load sample documents for the tenant workspace",
    )


class TenantResponse(BaseModel):
    id: uuid.UUID
    workspace_name: str
    slug: str
    business_category: Optional[str]
    employee_count_range: Optional[str]
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}

