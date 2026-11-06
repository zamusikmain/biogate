import logging
import sqlite3

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
    UploadFile,
)

from app.core.config import Settings
from app.schemas.api import (
    HealthResponse,
)

router = APIRouter()
logger = logging.getLogger(__name__)
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}


async def read_images(files: list[UploadFile], settings: Settings) -> list[bytes]:
    payloads: list[bytes] = []
    for upload in files:
        if upload.content_type not in ALLOWED_MIME:
            raise HTTPException(
                415,
                detail={
                    "reason_code": "UNSUPPORTED_MEDIA_TYPE",
                    "message": "JPEG, PNG or WebP required",
                },
            )
        data = await upload.read(settings.max_image_size + 1)
        await upload.close()
        if not data or len(data) > settings.max_image_size:
            raise HTTPException(
                413,
                detail={
                    "reason_code": "IMAGE_TOO_LARGE",
                    "message": "Image is empty or exceeds size limit",
                },
            )
        payloads.append(data)
    return payloads


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
