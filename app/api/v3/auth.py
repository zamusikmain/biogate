"""Password, face and recovery authentication and the caller's own sessions."""

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from starlette.concurrency import run_in_threadpool

from app.api.dependencies import (
    get_account_auth_service,
    get_app_settings,
    get_evidence_storage,
    get_face_login_service,
    get_pipeline,
    get_repository,
    require_account_csrf,
    require_account_session,
    require_full_account_session,
)
from app.api.v3._common import _account_payload, _client, _set_account_cookie, logger
from app.core.config import Settings
from app.schemas.v3 import (
    PasswordChangeRequest,
    PasswordResetRequest,
    RecoveryStartRequest,
)
from app.services.account_auth import ACCOUNT_COOKIE_NAME, AccountAuthError, AccountAuthService, AccountSession
from app.services.biometrics.errors import BiometricError
from app.services.biometrics.pipeline import BiometricPipeline
from app.services.face_login import FaceLoginService
from app.services.repository import Repository
from app.services.storage import EvidenceStorage

router = APIRouter()


@router.post("/auth/password")
async def password_login(
    request: Request,
    response: Response,
    auth: Annotated[AccountAuthService, Depends(get_account_auth_service)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    login = payload.get("login") if isinstance(payload, dict) else None
    password = payload.get("password") if isinstance(payload, dict) else None
    if not isinstance(login, str) or not 1 <= len(login) <= 100 or not isinstance(password, str) or not password:
        raise HTTPException(401, detail={"reason_code": "INVALID_CREDENTIALS"})
    address, agent = _client(request)
    try:
        # Argon2 verification is deliberately slow; keep it off the event loop.
        session, cookie = await run_in_threadpool(
            auth.password_login, login, password, client_address=address, user_agent=agent
        )
    except AccountAuthError as error:
        status = (
            429
            if error.reason_code == "LOGIN_RATE_LIMITED"
            else 403
            if error.reason_code.startswith("ACCOUNT_")
            else 401
        )
        raise HTTPException(status, detail={"reason_code": error.reason_code}) from error
    _set_account_cookie(response, cookie, settings)
    return _account_payload(session)


@router.get("/auth/me")
def account_me(
    response: Response,
    session: Annotated[AccountSession, Depends(require_account_session)],
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return _account_payload(session)


@router.post("/auth/logout", status_code=204)
def account_logout(
    response: Response,
    session: Annotated[AccountSession, Depends(require_account_csrf)],
    repository: Annotated[Repository, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> None:
    repository.revoke_session(session.id, "LOGOUT", expected_user_id=session.user_id)
    repository.add_audit("LOGOUT", session.user_id, "SUCCESS", {"session_id": session.id})
    response.delete_cookie(ACCOUNT_COOKIE_NAME, path="/", secure=settings.admin_cookie_secure, samesite="strict")


@router.post("/auth/change-password", status_code=204)
def change_password(
    payload: PasswordChangeRequest,
    session: Annotated[AccountSession, Depends(require_account_csrf)],
    auth: Annotated[AccountAuthService, Depends(get_account_auth_service)],
) -> None:
    try:
        auth.change_password(
            session,
            payload.current_password.get_secret_value(),
            payload.new_password.get_secret_value(),
        )
    except AccountAuthError as error:
        raise HTTPException(400, detail={"reason_code": error.reason_code}) from error


@router.get("/sessions")
def own_sessions(
    session: Annotated[AccountSession, Depends(require_full_account_session)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> list[dict[str, Any]]:
    return repository.list_account_sessions(session.user_id)


@router.delete("/sessions/{session_id}")
def revoke_own_session(
    session_id: str,
    session: Annotated[AccountSession, Depends(require_full_account_session)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, bool]:
    if session_id == session.id:
        raise HTTPException(409, detail={"reason_code": "USE_LOGOUT_FOR_CURRENT_SESSION"})
    if not repository.revoke_session(session_id, "USER_REVOKED", expected_user_id=session.user_id):
        raise HTTPException(404, detail={"reason_code": "SESSION_NOT_FOUND"})
    repository.add_security_event("SESSION_REVOKED", "INFO", session.user_id, {"session_id": session_id})
    return {"revoked": True}


@router.post("/face/challenges")
def create_face_challenge(
    request: Request,
    service: Annotated[FaceLoginService, Depends(get_face_login_service)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, Any]:
    address, agent = _client(request)
    try:
        return service.create("IDENTIFY", client_address=address)
    except ValueError as error:
        raise HTTPException(403, detail={"reason_code": str(error)}) from error
    except PermissionError as error:
        reason = str(error)
        if reason == "FACE_LOGIN_RATE_LIMITED":
            repository.add_access_event(
                {
                    "method": "FACE",
                    "result": "DENIED",
                    "reason": reason,
                    "client_address": address,
                    "user_agent": agent,
                }
            )
        raise HTTPException(429, detail={"reason_code": reason}, headers={"Retry-After": "60"}) from error


@router.post("/recovery/challenges")
def create_recovery_challenge(
    payload: RecoveryStartRequest,
    request: Request,
    service: Annotated[FaceLoginService, Depends(get_face_login_service)],
) -> dict[str, Any]:
    # Same response shape whether the account exists or is eligible.
    try:
        return service.create("RECOVERY", payload.login, _client(request)[0])
    except PermissionError as error:
        raise HTTPException(429, detail={"reason_code": str(error)}) from error


@router.post("/face/challenges/{challenge_id}/frames")
def analyze_face_challenge(
    challenge_id: str,
    request: Request,
    response: Response,
    frame: Annotated[UploadFile, File()],
    service: Annotated[FaceLoginService, Depends(get_face_login_service)],
    pipeline: Annotated[BiometricPipeline, Depends(get_pipeline)],
    repository: Annotated[Repository, Depends(get_repository)],
    auth: Annotated[AccountAuthService, Depends(get_account_auth_service)],
    evidence: Annotated[EvidenceStorage, Depends(get_evidence_storage)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    content_type = (frame.content_type or "").lower()
    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(415, detail={"reason_code": "UNSUPPORTED_MEDIA_TYPE"})
    try:
        data = frame.file.read(settings.max_image_size + 1)
    finally:
        frame.file.close()
    if len(data) > settings.max_image_size:
        raise HTTPException(413, detail={"reason_code": "IMAGE_TOO_LARGE"})
    try:
        started = time.perf_counter()
        action = service.expected_action(challenge_id)
        analysis = pipeline.analyze_liveness_frame(data, analyze_eyes=action == "BLINK")
        analysis_duration_ms = (time.perf_counter() - started) * 1000
        result = service.analyze(challenge_id, analysis, data, lambda: pipeline.extract_embedding(data))
        total_duration_ms = (time.perf_counter() - started) * 1000
        if settings.debug:
            debug = result.setdefault("debug", {})
            debug["analysisDurationMs"] = round(analysis_duration_ms, 2)
            debug["backendDurationMs"] = round(total_duration_ms, 2)
            logger.debug(
                "Liveness frame analyzed",
                extra={
                    "operation": "liveness_frame",
                    "duration_ms": round(total_duration_ms, 2),
                    "inference_time_ms": round(analysis_duration_ms, 2),
                },
            )
    except BiometricError as error:
        raise HTTPException(422, detail={"reason_code": error.reason_code}) from error
    except KeyError as error:
        raise HTTPException(404, detail={"reason_code": str(error.args[0])}) from error
    except TimeoutError as error:
        raise HTTPException(410, detail={"reason_code": str(error)}) from error
    if not result.get("completed"):
        return result
    address, agent = _client(request)
    decision = str(result["decision"])
    user_id = result.pop("_user_id", None)
    evidence_data = result.pop("_evidence", None)
    security_metadata = result.pop("_security_metadata", {})
    session: AccountSession | None = None
    cookie = None
    if decision == "IDENTIFIED" and user_id is not None:
        user = repository.get_user_by_id(int(user_id))
        if user:
            session, cookie = auth.create_session(user, "FACE", address, agent)
            repository.record_login_success(int(user_id), "FACE")
    event_result = "SUCCESS" if decision == "IDENTIFIED" else decision
    event_id = repository.add_access_event(
        {
            "user_id": user_id,
            "method": "FACE",
            "result": event_result,
            "reason": decision,
            "session_id": session.id if session else None,
            "liveness_score": 1.0,
            "similarity": result.get("best_similarity", result.get("similarity")),
            "second_similarity": result.get("second_similarity"),
            "client_address": address,
            "user_agent": agent,
        }
    )
    if evidence_data:
        evidence.save(
            evidence_data,
            "image/jpeg",
            f"FACE_{decision}",
            user_id=int(user_id) if user_id is not None else None,
            access_event_id=event_id,
        )
    if decision == "IDENTIFIED" and session and cookie:
        _set_account_cookie(response, cookie, settings)
        result["account"] = _account_payload(session)
        repository.add_audit("FACE_LOGIN_SUCCESS", session.user_id, "SUCCESS", {"access_event_id": event_id})
        repository.add_notification(session.user_id, None, "LOGIN", "FACE_LOGIN_SUCCESS")
    elif decision == "RECOVERY_VERIFIED" and user_id is not None:
        result["reset_authorization"] = auth.issue_reset_authorization(int(user_id))
    else:
        event_type = {
            "UNKNOWN": "UNKNOWN_FACE",
            "AMBIGUOUS": "AMBIGUOUS_FACE",
            "BLOCKED": "BLOCKED_USER_ATTEMPT",
            "DISABLED": "DISABLED_USER_ATTEMPT",
            "DENIED": "PASSWORD_RECOVERY_FAILED",
        }.get(decision, "MULTIPLE_FAILED_FACE_ATTEMPTS")
        repository.add_security_event(
            event_type,
            "WARNING",
            int(user_id) if user_id else None,
            security_metadata,
            event_id,
        )
        repository.add_notification(
            None,
            "ADMIN",
            event_type,
            event_type,
            {"access_event_id": event_id},
        )
    return result


@router.post("/recovery/reset", status_code=204)
def complete_recovery(
    payload: PasswordResetRequest,
    auth: Annotated[AccountAuthService, Depends(get_account_auth_service)],
) -> None:
    try:
        auth.consume_reset_authorization(payload.token.get_secret_value(), payload.new_password.get_secret_value())
    except AccountAuthError as error:
        raise HTTPException(400, detail={"reason_code": error.reason_code}) from error
