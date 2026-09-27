import logging
import sqlite3
import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)

from app.api.dependencies import (
    get_app_settings,
    get_pipeline,
    get_repository,
    require_admin_csrf,
    require_admin_session,
)
from app.core.config import Settings
from app.schemas.api import (
    DeleteResponse,
    EnrollmentResponse,
    HealthResponse,
    StatsResponse,
    UserResponse,
    VerificationAttemptResponse,
    VerificationResponse,
)
from app.services.biometrics.embedding import deserialize_embedding, serialize_embedding
from app.services.biometrics.errors import BiometricError
from app.services.biometrics.pipeline import BiometricPipeline
from app.services.repository import Repository

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


@router.post(
    "/api/enroll",
    response_model=EnrollmentResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin_session), Depends(require_admin_csrf)],
)
async def enroll(
    external_id: Annotated[str, Form(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")],
    display_name: Annotated[str, Form(min_length=1, max_length=100)],
    frames: Annotated[list[UploadFile], File()],
    repository: Annotated[Repository, Depends(get_repository)],
    pipeline: Annotated[BiometricPipeline, Depends(get_pipeline)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> EnrollmentResponse:
    if repository.get_user(external_id):
        raise HTTPException(409, detail={"reason_code": "ALREADY_ENROLLED", "message": "User ID already exists"})
    if not settings.min_enrollment_frames <= len(frames) <= settings.max_enrollment_frames:
        raise HTTPException(
            422,
            detail={
                "reason_code": "INVALID_FRAME_COUNT",
                "message": f"Provide {settings.min_enrollment_frames}-{settings.max_enrollment_frames} frames",
            },
        )
    payloads = await read_images(frames, settings)
    try:
        template, quality = pipeline.create_template(payloads)
        user = repository.enroll(
            external_id,
            display_name.strip(),
            pipeline.model_name,
            pipeline.model_version,
            serialize_embedding(template),
            template.size,
        )
    except BiometricError as error:
        raise HTTPException(422, detail={"reason_code": error.reason_code, "message": error.message}) from error
    except sqlite3.IntegrityError as error:
        raise HTTPException(
            409, detail={"reason_code": "ALREADY_ENROLLED", "message": "User ID already exists"}
        ) from error
    logger.info("Enrollment completed", extra={"operation": "enroll", "decision": "SUCCESS"})
    return EnrollmentResponse(
        status="ENROLLED",
        user=UserResponse(**user),
        accepted_frames=len(payloads),
        quality_score=quality,
        model_name=f"{pipeline.model_name}-{pipeline.model_version}",
    )


@router.post("/api/verify", response_model=VerificationResponse)
async def verify(
    external_id: Annotated[str, Form(min_length=1, max_length=64)],
    frames: Annotated[list[UploadFile], File()],
    repository: Annotated[Repository, Depends(get_repository)],
    pipeline: Annotated[BiometricPipeline, Depends(get_pipeline)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> VerificationResponse:
    started = time.perf_counter()
    user = repository.get_user(external_id)
    if not user or not repository.get_template(int(user["id"])):
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        repository.add_verification(
            {
                "user_id": user["id"] if user else None,
                "claimed_external_id": external_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "face_detected": 0,
                "quality_score": None,
                "liveness_score": None,
                "similarity_score": None,
                "threshold": settings.verification_threshold,
                "result": "REJECTED",
                "reason_code": "BIOMETRIC_NOT_ENROLLED",
                "inference_time_ms": elapsed,
            }
        )
        raise HTTPException(
            404,
            detail={
                "reason_code": "BIOMETRIC_NOT_ENROLLED",
                "message": "No biometric enrollment exists",
            },
        )
    if user.get("status") == "BLOCKED":
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        repository.add_verification(
            {
                "user_id": user["id"],
                "claimed_external_id": external_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "face_detected": 0,
                "quality_score": None,
                "liveness_score": None,
                "similarity_score": None,
                "threshold": settings.verification_threshold,
                "result": "REJECTED",
                "reason_code": "ACCOUNT_BLOCKED",
                "inference_time_ms": elapsed,
            }
        )
        raise HTTPException(403, detail={"reason_code": "ACCOUNT_BLOCKED", "message": "Account is blocked"})
    if not settings.min_enrollment_frames <= len(frames) <= settings.max_enrollment_frames:
        raise HTTPException(
            422,
            detail={
                "reason_code": "INVALID_FRAME_COUNT",
                "message": f"Provide {settings.min_enrollment_frames}-{settings.max_enrollment_frames} frames",
            },
        )
    payloads = await read_images(frames, settings)
    face_detected, quality_score, live_score, similarity = False, None, None, None
    result, reason = "REJECTED", "INFERENCE_ERROR"
    try:
        record = repository.get_template(user["id"])
        if not record:
            raise BiometricError("BIOMETRIC_NOT_ENROLLED", "No biometric template exists")
        template = deserialize_embedding(record["embedding"], record["embedding_dimensions"])
        similarity, quality_score, liveness = pipeline.verify(payloads, template)
        face_detected = True
        live_score = liveness.score
        if not liveness.passed:
            reason = liveness.reason_code
        elif similarity >= settings.verification_threshold:
            result, reason = "VERIFIED", "MATCH"
        else:
            reason = "FACE_MISMATCH"
    except BiometricError as error:
        reason = error.reason_code
    except Exception:
        logger.exception("Biometric inference failed", extra={"operation": "verify", "reason_code": reason})
    elapsed = round((time.perf_counter() - started) * 1000, 2)
    repository.add_verification(
        {
            "user_id": user["id"],
            "claimed_external_id": external_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "face_detected": int(face_detected),
            "quality_score": quality_score,
            "liveness_score": live_score,
            "similarity_score": similarity,
            "threshold": settings.verification_threshold,
            "result": result,
            "reason_code": reason,
            "inference_time_ms": elapsed,
        }
    )
    logger.info(
        "Verification completed",
        extra={
            "operation": "verify",
            "decision": result,
            "reason_code": reason,
            "inference_time_ms": elapsed,
        },
    )
    return VerificationResponse(
        face_detected=face_detected,
        image_quality=quality_score is not None,
        liveness=reason not in {"LIVENESS_FAILED", "LIVENESS_INSUFFICIENT_FRAMES"} and live_score is not None,
        liveness_score=live_score,
        similarity_score=similarity,
        threshold=settings.verification_threshold,
        inference_time_ms=elapsed,
        result=result,
        reason_code=reason,
    )


@router.get("/api/users", response_model=list[UserResponse])
def users(repository: Annotated[Repository, Depends(get_repository)]) -> list[UserResponse]:
    return [UserResponse(**item) for item in repository.list_users()]


@router.get("/api/users/{user_id}", response_model=UserResponse)
def user(user_id: str, repository: Annotated[Repository, Depends(get_repository)]) -> UserResponse:
    item = repository.get_user(user_id)
    if not item:
        raise HTTPException(404, detail={"reason_code": "USER_NOT_FOUND", "message": "User not found"})
    return UserResponse(**item)


@router.delete(
    "/api/users/{user_id}/biometric",
    response_model=DeleteResponse,
    dependencies=[Depends(require_admin_session), Depends(require_admin_csrf)],
)
def delete_biometric(user_id: str, repository: Annotated[Repository, Depends(get_repository)]) -> DeleteResponse:
    if not repository.get_user(user_id):
        raise HTTPException(404, detail={"reason_code": "USER_NOT_FOUND", "message": "User not found"})
    if not repository.delete_biometric(user_id):
        raise HTTPException(
            409,
            detail={"reason_code": "BIOMETRIC_NOT_ENROLLED", "message": "No biometric enrollment exists"},
        )
    return DeleteResponse(deleted=True)


@router.get(
    "/api/verifications",
    response_model=list[VerificationAttemptResponse],
    dependencies=[Depends(require_admin_session)],
)
def verifications(
    repository: Annotated[Repository, Depends(get_repository)],
    limit: int = Query(100, ge=1, le=500),
) -> list[VerificationAttemptResponse]:
    return [VerificationAttemptResponse(**item) for item in repository.verifications(limit)]


@router.get("/api/stats", response_model=StatsResponse, dependencies=[Depends(require_admin_session)])
def stats(repository: Annotated[Repository, Depends(get_repository)]) -> StatsResponse:
    raw = repository.stats()
    return StatsResponse(
        enrolled_users=raw.get("enrolled_users") or 0,
        total_verifications=raw.get("total_verifications") or 0,
        successful_verifications=raw.get("successful_verifications") or 0,
        rejected_verifications=raw.get("rejected_verifications") or 0,
        average_inference_time_ms=round(raw.get("average_inference_time_ms") or 0, 2),
    )
