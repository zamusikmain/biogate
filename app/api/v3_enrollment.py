"""Administrator-driven biometric enrollment for an account (photo upload or webcam).

Replaces the legacy /api/admin/users/{external_id}/enrollment/* endpoints; accounts
are addressed by their numeric id like the rest of /api/v3/admin.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.api.dependencies import (
    get_app_settings,
    get_identification_service,
    get_pipeline,
    get_repository,
    require_account_admin,
)
from app.core.config import Settings
from app.schemas.api import EnrollmentFrameValidationResponse, PhotoEnrollmentResponse, PhotoResult
from app.services.account_auth import AccountSession
from app.services.biometrics.embedding import aggregate, select_representatives, serialize_embedding
from app.services.biometrics.errors import BiometricError
from app.services.biometrics.pipeline import BiometricPipeline
from app.services.identification import IdentificationService
from app.services.repository import Repository

ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}

router = APIRouter(prefix="/api/v3/admin/accounts", tags=["BioGate V3"])


def read_enrollment_photo(photo: UploadFile, settings: Settings) -> bytes:
    """Read one enrollment image into memory and always close the upload.

    Synchronous on purpose: callers are plain ``def`` endpoints that FastAPI runs in its
    thread pool, so image analysis never blocks the event loop.
    """
    try:
        if photo.content_type not in ALLOWED_MIME:
            raise BiometricError("UNSUPPORTED_MEDIA_TYPE", "JPEG, PNG or WebP required")
        payload = photo.file.read(settings.max_image_size + 1)
        if not payload:
            raise BiometricError("INVALID_IMAGE", "Image is empty")
        if len(payload) > settings.max_image_size:
            raise BiometricError("IMAGE_TOO_LARGE", "Image exceeds size limit")
        return payload
    finally:
        photo.file.close()


def _account(repository: Repository, user_id: int) -> dict[str, Any]:
    user = repository.get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, detail={"reason_code": "USER_NOT_FOUND"})
    return user


@router.post("/{user_id}/enrollment/validate", response_model=EnrollmentFrameValidationResponse)
def validate_enrollment_frame(
    user_id: int,
    photo: Annotated[UploadFile, File()],
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
    pipeline: Annotated[BiometricPipeline, Depends(get_pipeline)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> EnrollmentFrameValidationResponse:
    _account(repository, user_id)
    try:
        payload = read_enrollment_photo(photo, settings)
        frame = pipeline.process_enrollment_frame(payload)
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
        bounding_box={"x": x, "y": y, "width": width, "height": height},
    )


@router.post("/{user_id}/enrollment/photos", response_model=PhotoEnrollmentResponse)
def photo_enrollment(
    user_id: int,
    photos: Annotated[list[UploadFile], File()],
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
    pipeline: Annotated[BiometricPipeline, Depends(get_pipeline)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    identification: Annotated[IdentificationService, Depends(get_identification_service)],
) -> PhotoEnrollmentResponse:
    user = _account(repository, user_id)
    if not settings.min_enrollment_frames <= len(photos) <= settings.max_enrollment_frames:
        raise HTTPException(422, detail={"reason_code": "INVALID_FRAME_COUNT"})
    results: list[PhotoResult] = []
    embeddings = []
    for index, photo in enumerate(photos, 1):
        filename = photo.filename or f"photo-{index}"
        try:
            payload = read_enrollment_photo(photo, settings)
            frame = pipeline.process_enrollment_frame(payload)
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
    representatives = select_representatives(embeddings)
    duplicate = identification.possible_duplicate(template, excluding_user_id=user_id)
    if duplicate:
        repository.add_security_event(
            "DUPLICATE_BIOMETRIC_DETECTED",
            "WARNING",
            user_id,
            {"duplicate_user_id": duplicate.user_id, "similarity": duplicate.similarity},
        )
        raise HTTPException(409, detail={"reason_code": "DUPLICATE_BIOMETRIC"})
    repository.upsert_template(
        str(user["external_id"]),
        pipeline.model_name,
        pipeline.model_version,
        serialize_embedding(template),
        int(template.size),
        len(embeddings),
        [(serialize_embedding(sample), int(sample.size)) for sample in representatives],
    )
    return PhotoEnrollmentResponse(
        status="ENROLLED",
        accepted_frames=len(embeddings),
        required_frames=settings.min_enrollment_frames,
        template_created=True,
        results=results,
        model_name=f"{pipeline.model_name}-{pipeline.model_version}",
    )
