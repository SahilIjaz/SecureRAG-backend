"""
AI provider/model selection (/api/model-options, /api/settings/model).

  GET /api/model-options   -> available providers, live price + benefit tag
  GET /api/settings/model  -> the tenant's current selection
  PUT /api/settings/model  -> change it

See NexusContext plan doc i-want-to-implement-floofy-hickey.md section B.
Selecting null/None means "use the platform default chain" — today's
behavior, zero disruption for a tenant who never visits this page.
"""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.frontend import helpers
from app.config import settings as app_settings
from app.database import get_db
from app.models.user import User
from app.services.auth_service import get_current_user
from app.services.llm import pricing
from app.services.llm.provider_catalog import get_catalog_entry
from app.services.llm.router import KNOWN_PROVIDERS, _get_provider, health_check_all
from app.services import wallet_service

router = APIRouter(tags=["Frontend — AI Model"])


class FEModelOption(BaseModel):
    provider: str
    model: str
    displayName: str
    tagline: str
    description: str
    pricePerThousandTokensUsd: float | None  # None = trial-only / not currently priceable
    isDefault: bool
    isSelected: bool


class FEModelOptionsResponse(BaseModel):
    options: list[FEModelOption]
    walletBalanceUsd: float
    inTrial: bool


class FEModelPreference(BaseModel):
    provider: str | None
    model: str | None


@router.get("/model-options", response_model=FEModelOptionsResponse)
async def get_model_options(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEModelOptionsResponse:
    tenant = await helpers.get_tenant(current_user, db)
    selected_provider = tenant.preferred_llm_provider or wallet_service.DEFAULT_PROVIDER

    reachable = await health_check_all()
    options: list[FEModelOption] = []
    for provider_name in KNOWN_PROVIDERS:
        provider = _get_provider(provider_name)
        if provider is None:
            continue
        reachable_models = reachable.get(provider_name, [])
        if not reachable_models:
            continue  # unreachable/unconfigured — hide, don't offer a dead option
        model = reachable_models[0]
        catalog = get_catalog_entry(provider_name)

        # Live price shown = what it'll actually cost the tenant: raw cost
        # per 1K tokens (blended input/output at a representative 1:1 mix)
        # times the wallet markup — see app/services/llm/pricing.py and
        # app/services/wallet_service.py for the real per-call math.
        raw = pricing.estimate_cost_usd(provider_name, model, 500, 500)  # 1K-token sample
        price_per_k = (
            float(raw * Decimal(str(app_settings.WALLET_MARKUP_MULTIPLIER))) if raw is not None else None
        )

        options.append(FEModelOption(
            provider=provider_name,
            model=model,
            displayName=catalog["displayName"],
            tagline=catalog["tagline"],
            description=catalog["description"],
            pricePerThousandTokensUsd=price_per_k,
            isDefault=provider_name == wallet_service.DEFAULT_PROVIDER,
            isSelected=provider_name == selected_provider,
        ))

    return FEModelOptionsResponse(
        options=options,
        walletBalanceUsd=float(tenant.balance_usd or 0),
        inTrial=tenant.trial_ended_at is None,
    )


@router.get("/settings/model", response_model=FEModelPreference)
async def get_model_preference(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEModelPreference:
    tenant = await helpers.get_tenant(current_user, db)
    return FEModelPreference(provider=tenant.preferred_llm_provider, model=tenant.preferred_llm_model)


@router.put("/settings/model", response_model=FEModelPreference)
async def set_model_preference(
    body: FEModelPreference,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FEModelPreference:
    if body.provider is not None and body.provider not in KNOWN_PROVIDERS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown provider {body.provider!r}.")

    tenant = await helpers.get_tenant(current_user, db)
    tenant.preferred_llm_provider = body.provider
    tenant.preferred_llm_model = body.model
    await db.flush()
    return FEModelPreference(provider=tenant.preferred_llm_provider, model=tenant.preferred_llm_model)
