from fastapi import APIRouter

router = APIRouter()

# ---------------------------------------------------------------------------
# Future route includes go here, for example:
#   from app.api.v1.endpoints import auth, tenants, users, subscriptions
#   router.include_router(auth.router, prefix="/auth", tags=["auth"])
#   router.include_router(tenants.router, prefix="/tenants", tags=["tenants"])
#   router.include_router(users.router, prefix="/users", tags=["users"])
#   router.include_router(subscriptions.router, prefix="/subscriptions", tags=["subscriptions"])
# ---------------------------------------------------------------------------


@router.get("/ping", tags=["health"], summary="Liveness check")
async def ping() -> dict:
    """Return a simple pong response to confirm the API v1 router is reachable."""
    return {"message": "pong"}
