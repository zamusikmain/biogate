import json
import sqlite3
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from app.api.dependencies import (
    get_app_settings,
    get_pipeline,
    get_repository,
    require_admin_csrf,
    require_admin_session,
)
from app.api.routes import ALLOWED_MIME
from app.core.config import Settings
from app.schemas.api import (
    DeleteResponse,
    EnrollmentFrameValidationResponse,
    PhotoEnrollmentResponse,
    PhotoResult,
    SettingUpdateRequest,
    UserCreateRequest,
    UserResponse,
    UserUpdateRequest,
)
from app.services.biometrics.embedding import aggregate, serialize_embedding
from app.services.biometrics.errors import BiometricError
from app.services.biometrics.pipeline import BiometricPipeline
from app.services.repository import Repository

router = APIRouter(
    prefix="/api/admin",
    tags=["Administration"],
    dependencies=[Depends(require_admin_session), Depends(require_admin_csrf)],
)


async def read_enrollment_photo(photo: UploadFile, settings: Settings) -> bytes:
    """Read one enrollment image into memory and always close the upload."""
    try:
        if photo.content_type not in ALLOWED_MIME:
            raise BiometricError("UNSUPPORTED_MEDIA_TYPE", "JPEG, PNG or WebP required")
        payload = await photo.read(settings.max_image_size + 1)
        if not payload:
            raise BiometricError("INVALID_IMAGE", "Image is empty")
        if len(payload) > settings.max_image_size:
            raise BiometricError("IMAGE_TOO_LARGE", "Image exceeds size limit")
        return payload
    finally:
        await photo.close()


@router.get("/dashboard")
def dashboard(repository: Annotated[Repository, Depends(get_repository)]) -> dict[str, Any]:
    return {"stats": repository.admin_stats(), "recent_events": repository.audit_events(12)}


@router.post("/users", response_model=UserResponse, status_code=201)
def create_user(request: UserCreateRequest, repository: Annotated[Repository, Depends(get_repository)]) -> UserResponse:
    try:
        return UserResponse(**repository.create_user(**request.model_dump()))
    except sqlite3.IntegrityError as error:
        raise HTTPException(409, detail={"reason_code": "USER_ALREADY_EXISTS"}) from error


@router.get("/users")
def list_users(repository: Annotated[Repository, Depends(get_repository)]) -> list[dict[str, Any]]:
    return repository.list_users()


@router.get("/users/{external_id}")
def user_detail(external_id: str, repository: Annotated[Repository, Depends(get_repository)]) -> dict[str, Any]:
    item = repository.user_detail(external_id)
    if not item:
        raise HTTPException(404, detail={"reason_code": "USER_NOT_FOUND"})
    return item


@router.patch("/users/{external_id}")
def update_user(
    external_id: str,
    request: UserUpdateRequest,
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, Any]:
    item = repository.update_user(external_id, **request.model_dump())
    if not item:
        raise HTTPException(404, detail={"reason_code": "USER_NOT_FOUND"})
    return item


@router.delete("/users/{external_id}", response_model=DeleteResponse)
def delete_user(external_id: str, repository: Annotated[Repository, Depends(get_repository)]) -> DeleteResponse:
    if not repository.delete_user(external_id):
        raise HTTPException(404, detail={"reason_code": "USER_NOT_FOUND"})
    return DeleteResponse(deleted=True)


@router.delete("/users/{external_id}/biometric", response_model=DeleteResponse)
def delete_biometric(external_id: str, repository: Annotated[Repository, Depends(get_repository)]) -> DeleteResponse:
    if not repository.get_user(external_id):
        raise HTTPException(404, detail={"reason_code": "USER_NOT_FOUND"})
    if not repository.delete_biometric(external_id):
        raise HTTPException(409, detail={"reason_code": "BIOMETRIC_NOT_ENROLLED"})
    return DeleteResponse(deleted=True)


@router.post(
    "/users/{external_id}/enrollment/validate",
    response_model=EnrollmentFrameValidationResponse,
)
async def validate_enrollment_frame(
    external_id: str,
    photo: Annotated[UploadFile, File()],
    repository: Annotated[Repository, Depends(get_repository)],
    pipeline: Annotated[BiometricPipeline, Depends(get_pipeline)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> EnrollmentFrameValidationResponse:
    if not repository.get_user(external_id):
        raise HTTPException(404, detail={"reason_code": "USER_NOT_FOUND"})
    try:
        payload = await read_enrollment_photo(photo, settings)
        frame = pipeline.process_frame(payload)
    except BiometricError as error:
        return EnrollmentFrameValidationResponse(accepted=False, reason_code=error.reason_code)
    x, y, width, height = frame.observation.box
    # No image or embedding is retained; these normalized geometry values only
    # let the browser require a short stable window before accepting a frame.
    return EnrollmentFrameValidationResponse(
        accepted=True,
        reason_code="OK",
        quality_score=frame.quality.score,
        yaw=frame.observation.yaw_proxy,
        bounding_box={
            "x": x,
            "y": y,
            "width": width,
            "height": height,
        },
    )


@router.post("/users/{external_id}/enrollment/photos", response_model=PhotoEnrollmentResponse)
async def photo_enrollment(
    external_id: str,
    photos: Annotated[list[UploadFile], File()],
    repository: Annotated[Repository, Depends(get_repository)],
    pipeline: Annotated[BiometricPipeline, Depends(get_pipeline)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> PhotoEnrollmentResponse:
    if not repository.get_user(external_id):
        raise HTTPException(404, detail={"reason_code": "USER_NOT_FOUND"})
    if not settings.min_enrollment_frames <= len(photos) <= settings.max_enrollment_frames:
        raise HTTPException(422, detail={"reason_code": "INVALID_FRAME_COUNT"})
    filenames = [photo.filename or f"photo-{index + 1}" for index, photo in enumerate(photos)]
    results: list[PhotoResult] = []
    embeddings = []
    for index, (filename, photo) in enumerate(zip(filenames, photos, strict=True), 1):
        try:
            payload = await read_enrollment_photo(photo, settings)
            frame = pipeline.process_frame(payload)
            embeddings.append(frame.embedding)
            results.append(
                PhotoResult(
                    index=index, filename=filename, accepted=True, reason_code="OK", quality_score=frame.quality.score
                )
            )
        except BiometricError as error:
            results.append(PhotoResult(index=index, filename=filename, accepted=False, reason_code=error.reason_code))
    if len(embeddings) < settings.min_enrollment_frames:
        return PhotoEnrollmentResponse(
            status="INSUFFICIENT_VALID_IMAGES",
            accepted_frames=len(embeddings),
            required_frames=settings.min_enrollment_frames,
            template_created=False,
            results=results,
        )
    template = aggregate(embeddings)
    repository.upsert_template(
        external_id,
        pipeline.model_name,
        pipeline.model_version,
        serialize_embedding(template),
        int(template.size),
        len(embeddings),
    )
    return PhotoEnrollmentResponse(
        status="ENROLLED",
        accepted_frames=len(embeddings),
        required_frames=settings.min_enrollment_frames,
        template_created=True,
        results=results,
        model_name=f"{pipeline.model_name}-{pipeline.model_version}",
    )


@router.get("/users/{external_id}/verifications")
def user_history(
    external_id: str,
    repository: Annotated[Repository, Depends(get_repository)],
    result: str | None = None,
    reason: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return repository.user_verifications(external_id, result, reason, limit)


@router.get("/verifications")
def global_history(
    repository: Annotated[Repository, Depends(get_repository)],
    limit: int = Query(200, ge=1, le=500),
    user: str | None = None,
    result: str | None = None,
    reason: str | None = None,
    liveness: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    return repository.filtered_verifications(limit, user, result, reason, liveness, date_from, date_to)


@router.get("/verifications/{attempt_id}")
def attempt_detail(attempt_id: int, repository: Annotated[Repository, Depends(get_repository)]) -> dict[str, Any]:
    attempt = repository.get_verification(attempt_id)
    if not attempt:
        raise HTTPException(404, detail={"reason_code": "ATTEMPT_NOT_FOUND"})
    attempt["audit_events"] = []
    for event in repository.audit_events(500):
        metadata = json.loads(event.get("metadata") or "{}")
        if event.get("attempt_id") == attempt_id or metadata.get("attempt_id") == attempt_id:
            attempt["audit_events"].append(event)
    return attempt


@router.get("/verifications/{attempt_id}/snapshot", response_class=FileResponse)
def attempt_snapshot(
    attempt_id: int,
    repository: Annotated[Repository, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> FileResponse:
    snapshot = repository.get_snapshot(attempt_id)
    if not snapshot:
        raise HTTPException(404, detail={"reason_code": "SNAPSHOT_NOT_FOUND"})
    path = (settings.attempt_image_dir / snapshot["file_name"]).resolve()
    if path.parent != settings.attempt_image_dir.resolve() or not path.is_file():
        raise HTTPException(404, detail={"reason_code": "SNAPSHOT_NOT_FOUND"})
    return FileResponse(path, media_type="image/jpeg", filename="attempt-snapshot.jpg")


@router.delete("/verifications/{attempt_id}/snapshot", response_model=DeleteResponse)
def delete_snapshot(
    attempt_id: int,
    repository: Annotated[Repository, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> DeleteResponse:
    snapshot = repository.delete_snapshot(attempt_id)
    if not snapshot:
        raise HTTPException(404, detail={"reason_code": "SNAPSHOT_NOT_FOUND"})
    (settings.attempt_image_dir / snapshot["file_name"]).unlink(missing_ok=True)
    return DeleteResponse(deleted=True)


@router.post("/snapshots/cleanup")
def cleanup_snapshots(
    repository: Annotated[Repository, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict[str, int]:
    deleted = 0
    for snapshot in repository.expired_snapshots(datetime.now(UTC).isoformat()):
        (settings.attempt_image_dir / snapshot["file_name"]).unlink(missing_ok=True)
        repository.delete_snapshot(int(snapshot["attempt_id"]))
        deleted += 1
    return {"deleted": deleted}


@router.get("/audit")
def audit_log(
    repository: Annotated[Repository, Depends(get_repository)],
    limit: int = Query(200, ge=1, le=500),
) -> list[dict[str, Any]]:
    return repository.audit_events(limit)


@router.get("/settings")
def get_settings(
    repository: Annotated[Repository, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict[str, Any]:
    stored = repository.get_setting("store_attempt_images")
    return {
        "store_attempt_images": settings.store_attempt_images if stored is None else stored == "true",
        "retention_days": settings.attempt_image_retention_days,
        "snapshot_directory": "private local storage",
    }


@router.put("/settings")
def update_settings(
    request: SettingUpdateRequest, repository: Annotated[Repository, Depends(get_repository)]
) -> dict[str, bool]:
    repository.set_setting("store_attempt_images", str(request.store_attempt_images).lower())
    return {"store_attempt_images": request.store_attempt_images}
