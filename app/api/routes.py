import logging
import sqlite3

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
)

from app.schemas.api import (
    HealthResponse,
)

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="BioGate",
        database="ok",
        model_loaded=hasattr(request.app.state, "pipeline"),
    )


@router.get("/health/live", include_in_schema=False)
def health_live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", include_in_schema=False)
def health_ready(request: Request) -> dict[str, str]:
    try:
        with request.app.state.repository.db.connect() as connection:
            database_ok = connection.execute("SELECT 1").fetchone()[0] == 1
        settings = request.app.state.settings
        storage_ok = all(path.exists() and path.is_dir() for path in (settings.active_image_dir, settings.archive_dir))
        model_ok = hasattr(request.app.state, "pipeline")
    except (AttributeError, OSError, sqlite3.DatabaseError):
        database_ok = storage_ok = model_ok = False
    if not (database_ok and storage_ok and model_ok):
        raise HTTPException(503, detail={"reason_code": "SERVICE_NOT_READY"})
    return {"status": "ready"}
