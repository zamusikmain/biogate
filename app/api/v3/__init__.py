"""BioGate V3 API under /api/v3, one router per area."""

from fastapi import APIRouter

from app.api.v3 import admin_accounts, admin_events, admin_system, auth, evidence, user
from app.api.v3._common import V3_RUNTIME_SETTINGS

__all__ = ["V3_RUNTIME_SETTINGS", "router"]

router = APIRouter(prefix="/api/v3", tags=["BioGate V3"])
router.include_router(auth.router)
router.include_router(user.router)
router.include_router(admin_accounts.router)
router.include_router(admin_events.router)
router.include_router(admin_system.router)
router.include_router(evidence.router)
