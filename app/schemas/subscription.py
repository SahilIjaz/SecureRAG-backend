import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.models.subscription import BillingCycle, PlanName


class PlanSelectionRequest(BaseModel):
    plan_name: PlanName = Field(..., examples=["free"])
    billing_cycle: Optional[BillingCycle] = Field(
        None,
        examples=["monthly"],
        description="Required when plan_name is not 'free'",
    )


class SubscriptionResponse(BaseModel):
    id: uuid.UUID
    plan_name: PlanName
    billing_cycle: Optional[BillingCycle]
    status: str
    started_at: datetime
    expires_at: Optional[datetime]

    model_config = {"from_attributes": True}


class TenantQuotaResponse(BaseModel):
    max_documents: int = Field(
        ..., description="Maximum number of documents allowed. -1 means unlimited."
    )
    max_file_size_mb: int = Field(
        ..., description="Maximum file upload size in MB. -1 means unlimited."
    )
    max_questions_per_month: int = Field(
        ..., description="Maximum questions per month. -1 means unlimited."
    )

    model_config = {"from_attributes": True}

