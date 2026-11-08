"""USER self-service: profile, own access history, notifications and biometric requests."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from app.api.dependencies import (
    get_app_settings,
    get_biometric_workflow,
    get_evidence_storage,
    get_pipeline,
    get_repository,
    require_full_account_session,
)
from app.api.v3_enrollment import read_enrollment_photo
from app.core.config import Settings
from app.services.account_auth import AccountSession
from app.services.biometric_workflow import BiometricWorkflowError, BiometricWorkflowService
from app.services.biometrics.embedding import aggregate
from app.services.biometrics.errors import BiometricError
from app.services.biometrics.pipeline import BiometricPipeline
from app.services.repository import Repository
from app.services.storage import EvidenceStorage

router = APIRouter()


@router.get("/user/profile")
def own_profile(
    session: Annotated[AccountSession, Depends(require_full_account_session)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, Any]:
    user = repository.get_user_by_id(session.user_id)
    if not user:
        raise HTTPException(404, detail={"reason_code": "USER_NOT_FOUND"})
    return {
        key: value for key, value in user.items() if key not in {"password_hash", "failed_login_count", "locked_until"}
    }


@router.get("/user/access-events")
def own_access_events(
    session: Annotated[AccountSession, Depends(require_full_account_session)],
    repository: Annotated[Repository, Depends(get_repository)],
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    return repository.access_events(user_id=session.user_id, limit=limit, offset=offset)


@router.get("/user/notifications")
def own_notifications(
    session: Annotated[AccountSession, Depends(require_full_account_session)],
    repository: Annotated[Repository, Depends(get_repository)],
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    return repository.notifications(session.user_id, session.role, limit, offset)


@router.post("/user/notifications/{notification_id}/read")
def read_notification(
    notification_id: int,
    session: Annotated[AccountSession, Depends(require_full_account_session)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, bool]:
    if not repository.mark_notification_read(notification_id, session.user_id, session.role):
        raise HTTPException(404, detail={"reason_code": "NOTIFICATION_NOT_FOUND"})
    return {"read": True}


@router.post("/user/biometric-frame")
def validate_own_biometric_frame(
    photo: Annotated[UploadFile, File()],
    _: Annotated[AccountSession, Depends(require_full_account_session)],
    pipeline: Annotated[BiometricPipeline, Depends(get_pipeline)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict[str, Any]:
    """Ephemeral capture feedback; never stores or approves a biometric template."""
    try:
        data = read_enrollment_photo(photo, settings)
        frame = pipeline.process_enrollment_frame(data)
    except BiometricError as error:
        return {"accepted": False, "reason_code": error.reason_code}
    x, y, width, height = frame.observation.box
    return {
        "accepted": True,
        "reason_code": "OK",
        "bounding_box": {"x": x, "y": y, "width": width, "height": height},
    }


@router.post("/user/biometric-requests")
def submit_biometric_request(
    photos: Annotated[list[UploadFile], File()],
    session: Annotated[AccountSession, Depends(require_full_account_session)],
    workflow: Annotated[BiometricWorkflowService, Depends(get_biometric_workflow)],
    pipeline: Annotated[BiometricPipeline, Depends(get_pipeline)],
    evidence: Annotated[EvidenceStorage, Depends(get_evidence_storage)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict[str, Any]:
    if not settings.min_enrollment_frames <= len(photos) <= settings.max_enrollment_frames:
        raise HTTPException(422, detail={"reason_code": "INVALID_FRAME_COUNT"})
    frames = []
    source_data: list[tuple[bytes, str]] = []
    for photo in photos:
        try:
            data = read_enrollment_photo(photo, settings)
            frames.append(pipeline.process_enrollment_frame(data))
            source_data.append((data, photo.content_type or "image/jpeg"))
        except BiometricError as error:
            raise HTTPException(422, detail={"reason_code": error.reason_code}) from error
    template = aggregate([item.embedding for item in frames])
    try:
        request_record = workflow.submit(
            session.user_id,
            template,
            pipeline.model_name,
            pipeline.model_version,
            len(frames),
            [item.embedding for item in frames],
        )
    except BiometricWorkflowError as error:
        raise HTTPException(409, detail={"reason_code": error.reason_code}) from error
    for data, mime in source_data:
        evidence.save(
            data,
            mime,
            "BIOMETRIC_REQUEST",
            user_id=session.user_id,
            biometric_request_id=str(request_record["id"]),
        )
    return request_record


@router.get("/user/biometric-request")
def current_biometric_request(
    session: Annotated[AccountSession, Depends(require_full_account_session)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, Any] | None:
    record = repository.active_biometric_request(session.user_id)
    if not record:
        return None
    return repository.biometric_request_detail(str(record["id"]))


@router.delete("/user/biometric-requests/{request_id}")
def cancel_biometric_request(
    request_id: str,
    session: Annotated[AccountSession, Depends(require_full_account_session)],
    workflow: Annotated[BiometricWorkflowService, Depends(get_biometric_workflow)],
) -> dict[str, bool]:
    try:
        workflow.cancel(request_id, session.user_id)
    except BiometricWorkflowError as error:
        raise HTTPException(409, detail={"reason_code": error.reason_code}) from error
    return {"cancelled": True}
