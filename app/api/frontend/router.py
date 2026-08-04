"""
Aggregate router for the frontend-compat API, mounted at /api.

Paths match Nexus-frontend/src/api/*.api.ts exactly:
/api/auth, /api/user, /api/onboarding, /api/dashboard, /api/chatbot,
/api/conversations, /api/knowledge, /api/settings
"""

from fastapi import APIRouter

from app.api.frontend import (
    auth,
    chatbot,
    conversations,
    dashboard,
    knowledge,
    onboarding,
    public_bot,
    settings,
    user,
)

router = APIRouter()

router.include_router(auth.router)
router.include_router(user.router)
router.include_router(onboarding.router)
router.include_router(dashboard.router)
router.include_router(chatbot.router)
router.include_router(conversations.router)
router.include_router(knowledge.router)
router.include_router(settings.router)
router.include_router(public_bot.router)
