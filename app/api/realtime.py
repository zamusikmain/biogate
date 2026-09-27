import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.api.dependencies import (
    get_app_settings,
    get_challenge_service,
    get_pipeline,
    get_repository,
)
from app.api.routes import read_images
from app.core.config import Settings
from app.schemas.api import ChallengeCreateRequest, ChallengeResponse, FrameStatusResponse
from app.services.biometrics.errors import BiometricError
from app.services.biometrics.pipeline import BiometricPipeline
from app.services.challenges import ChallengeService
from app.services.repository import Repository

router = APIRouter(prefix="/api/verification-sessions", tags=["Realtime verification"])


@router.post("", response_model=ChallengeResponse, status_code=201)
def create_session(
    request: ChallengeCreateRequest,
    challenges: Annotated[ChallengeService, Depends(get_challenge_service)],
    repository: Annotated[Repository, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> ChallengeResponse:
    try:
        session = challenges.create(request.external_id)
    except PermissionError as error:
        user = repository.get_user(request.external_id)
        repository.add_verification(
            {
                "user_id": user["id"] if user else None,
                "claimed_external_id": request.external_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "face_detected": 0,
                "quality_score": None,
                "liveness_score": None,
                "similarity_score": None,
                "threshold": settings.verification_threshold,
                "result": "REJECTED",
                "reason_code": "ACCOUNT_BLOCKED",
                "inference_time_ms": 0.0,
            }
        )
        repository.add_audit(
            "VERIFICATION_REJECTED", user["id"] if user else None, "REJECTED", {"reason_code": "ACCOUNT_BLOCKED"}
        )
        raise HTTPException(403, detail={"reason_code": "ACCOUNT_BLOCKED"}) from error
    except ValueError as error:
        raise HTTPException(404, detail={"reason_code": str(error)}) from error
    sequence = __import__("json").loads(session["expected_sequence"])
    return ChallengeResponse(
        challenge_id=session["id"],
        sequence=sequence,
        current_step=0,
        total_steps=len(sequence),
        expected_action=sequence[0],
        expires_at=session["expires_at"],
    )


@router.post("/{session_id}/frames", response_model=FrameStatusResponse, response_model_exclude_none=True)
async def analyze_frame(
    session_id: str,
    frame: Annotated[UploadFile, File()],
    pipeline: Annotated[BiometricPipeline, Depends(get_pipeline)],
    challenges: Annotated[ChallengeService, Depends(get_challenge_service)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> FrameStatusResponse:
    payload = (await read_images([frame], settings))[0]
    started = time.perf_counter()
    try:
        analysis = pipeline.analyze_frame(payload)
        elapsed = (time.perf_counter() - started) * 1000
        return FrameStatusResponse(**challenges.analyze(session_id, analysis, payload, elapsed))
    except BiometricError as error:
        raise HTTPException(422, detail={"reason_code": error.reason_code, "message": error.message}) from error
    except KeyError as error:
        raise HTTPException(404, detail={"reason_code": str(error.args[0])}) from error
    except TimeoutError as error:
        raise HTTPException(410, detail={"reason_code": str(error)}) from error
    except ValueError as error:
        raise HTTPException(409, detail={"reason_code": str(error)}) from error


@router.delete("/{session_id}", status_code=204)
def cancel_session(session_id: str, challenges: Annotated[ChallengeService, Depends(get_challenge_service)]) -> None:
    challenges.expire(session_id)
