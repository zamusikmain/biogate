import asyncio
import csv
import io
import json
import logging
import sqlite3
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app.api.admin import read_enrollment_photo
from app.api.dependencies import (
    get_account_auth_service,
    get_app_settings,
    get_backup_service,
    get_biometric_workflow,
    get_evidence_storage,
    get_face_login_service,
    get_pipeline,
    get_repository,
    require_account_admin,
    require_account_csrf,
    require_account_session,
    require_full_account_session,
)
from app.api.event_safety import safe_event_row
from app.core.config import Settings
from app.schemas.v3 import (
    AccountCreateRequest,
    AccountUpdateRequest,
    AdminNoteRequest,
    AdminPasswordResetRequest,
    AdminReasonRequest,
    AdminSettingsRequest,
    BackupRestoreRequest,
    BiometricReviewRequest,
    ForcePasswordChangeRequest,
    PasswordChangeRequest,
    PasswordResetRequest,
    RecoveryStartRequest,
    SecurityReviewRequest,
)
from app.services.account_auth import ACCOUNT_COOKIE_NAME, AccountAuthError, AccountAuthService, AccountSession
from app.services.biometric_workflow import BiometricWorkflowError, BiometricWorkflowService
from app.services.biometrics.embedding import aggregate
from app.services.biometrics.errors import BiometricError
from app.services.biometrics.pipeline import BiometricPipeline
from app.services.face_login import FaceLoginService
from app.services.repository import Repository
from app.services.risk import annotate_risk
from app.services.storage import BackupService, EvidenceStorage

logger = logging.getLogger("biogate.liveness")

router = APIRouter(prefix="/api/v3", tags=["BioGate V3"])

V3_RUNTIME_SETTINGS = (
    "allow_face_login",
    "allow_password_login",
    "account_session_ttl_seconds",
    "account_session_idle_seconds",
    "password_login_max_attempts",
    "password_min_length",
    "identification_threshold",
    "identification_ambiguity_margin",
    "duplicate_biometric_threshold",
    "recovery_max_attempts",
)

def _client(request: Request) -> tuple[str, str]:
    return (request.client.host if request.client else "local", request.headers.get("user-agent", "")[:300])


def _set_account_cookie(response: Response, cookie: str, settings: Settings) -> None:
    response.set_cookie(
        ACCOUNT_COOKIE_NAME,
        cookie,
        max_age=settings.account_session_ttl_seconds,
        httponly=True,
        secure=settings.admin_cookie_secure,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"


def _account_payload(session: AccountSession) -> dict[str, Any]:
    return {
        "authenticated": True,
        "session_id": session.id,
        "user_id": session.user_id,
        "login": session.login,
        "full_name": session.full_name,
        "role": session.role,
        "status": session.status,
        "login_method": session.login_method,
        "must_change_password": session.must_change_password,
        "csrf_token": session.csrf_token,
    }


def _account_display_identity(account: dict[str, Any]) -> str:
    """Return a human account identity without reviving legacy role placeholders."""
    full_name = str(account.get("full_name") or "").strip()
    login = str(account.get("login") or "").strip()
    if full_name and full_name.casefold() not in {"biogate administrator", "biogate user"}:
        return full_name
    return login or full_name or "UNKNOWN"


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
        session, cookie = auth.password_login(login, password, client_address=address, user_agent=agent)
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
    service: Annotated[FaceLoginService, Depends(get_face_login_service)],
) -> dict[str, Any]:
    try:
        return service.create("IDENTIFY")
    except ValueError as error:
        raise HTTPException(403, detail={"reason_code": str(error)}) from error


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
async def analyze_face_challenge(
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
    data = await frame.read(settings.max_image_size + 1)
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
async def validate_own_biometric_frame(
    photo: Annotated[UploadFile, File()],
    _: Annotated[AccountSession, Depends(require_full_account_session)],
    pipeline: Annotated[BiometricPipeline, Depends(get_pipeline)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict[str, Any]:
    """Ephemeral capture feedback; never stores or approves a biometric template."""
    try:
        data = await read_enrollment_photo(photo, settings)
        frame = pipeline.process_frame(data)
    except BiometricError as error:
        return {"accepted": False, "reason_code": error.reason_code}
    x, y, width, height = frame.observation.box
    return {
        "accepted": True,
        "reason_code": "OK",
        "bounding_box": {"x": x, "y": y, "width": width, "height": height},
    }


@router.post("/user/biometric-requests")
async def submit_biometric_request(
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
            data = await read_enrollment_photo(photo, settings)
            frames.append(pipeline.process_frame(data))
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


@router.post("/admin/accounts", status_code=201)
def create_account(
    payload: AccountCreateRequest,
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
    auth: Annotated[AccountAuthService, Depends(get_account_auth_service)],
) -> dict[str, Any]:
    password = payload.temporary_password.get_secret_value() if payload.temporary_password else None
    generated = None
    if payload.generate_temporary_password:
        generated = auth.generate_temporary_password()
        password = generated
    if not password:
        raise HTTPException(422, detail={"reason_code": "TEMPORARY_PASSWORD_REQUIRED"})
    try:
        password_hash = auth.hash_password(password)
        user = repository.create_account(
            {
                **payload.model_dump(exclude={"temporary_password", "generate_temporary_password"}),
                "password_hash": password_hash,
                "must_change_password": True,
            }
        )
    except AccountAuthError as error:
        raise HTTPException(422, detail={"reason_code": error.reason_code}) from error
    except sqlite3.IntegrityError as error:
        message = str(error).casefold()
        reason = (
            "LOGIN_ALREADY_EXISTS"
            if "users.login" in message
            else "EXTERNAL_ID_ALREADY_EXISTS"
            if "users.external_id" in message
            else "EMPLOYEE_ID_ALREADY_EXISTS"
            if "users.employee_id" in message
            else "ACCOUNT_ALREADY_EXISTS"
        )
        raise HTTPException(409, detail={"reason_code": reason}) from error
    public = {key: value for key, value in user.items() if key != "password_hash"}
    if generated:
        public["temporary_password"] = generated
    return public


@router.get("/admin/accounts")
def admin_accounts(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    search: str | None = None,
    status: str | None = Query(default=None, pattern=r"^(ACTIVE|BLOCKED|DISABLED)$"),
    role: str | None = Query(default=None, pattern=r"^(USER|ADMIN)$"),
    biometric: str | None = Query(default=None, pattern=r"^(ENROLLED|NOT_ENROLLED|UPDATE_PENDING)$"),
) -> list[dict[str, Any]]:
    return repository.list_accounts(limit, offset, search, status, role, biometric)


@router.get("/admin/accounts/{user_id}")
def admin_account(
    user_id: int,
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, Any]:
    user = repository.get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, detail={"reason_code": "USER_NOT_FOUND"})
    return {
        key: value for key, value in user.items() if key not in {"password_hash", "failed_login_count", "locked_until"}
    }


@router.patch("/admin/accounts/{user_id}")
def admin_update_account(
    user_id: int,
    payload: AccountUpdateRequest,
    admin: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, Any]:
    current = repository.get_user_by_id(user_id)
    if not current:
        raise HTTPException(404, detail={"reason_code": "USER_NOT_FOUND"})
    removes_active_admin = current.get("role") == "ADMIN" and current.get("status") == "ACTIVE" and (
        payload.role == "USER" or payload.status in {"BLOCKED", "DISABLED"}
    )
    if removes_active_admin and repository.active_admin_count(excluding_user_id=user_id) == 0:
        raise HTTPException(409, detail={"reason_code": "LAST_ACTIVE_ADMIN_REQUIRED"})
    enabled_password = (
        payload.password_enabled
        if payload.password_enabled is not None
        else bool(current.get("password_enabled", 1))
    )
    enabled_face = (
        payload.face_enabled if payload.face_enabled is not None else bool(current.get("face_enabled", 1))
    )
    if payload.status in {None, "ACTIVE"} and not enabled_password and not enabled_face:
        raise HTTPException(409, detail={"reason_code": "ACCOUNT_AUTH_METHOD_REQUIRED"})
    try:
        user = repository.update_account(user_id, payload.model_dump(exclude_unset=True))
    except ValueError as error:
        raise HTTPException(409, detail={"reason_code": str(error)}) from error
    except sqlite3.IntegrityError as error:
        message = str(error).casefold()
        reason = (
            "LOGIN_ALREADY_EXISTS"
            if "users.login" in message
            else "EMPLOYEE_ID_ALREADY_EXISTS"
            if "users.employee_id" in message
            else "ACCOUNT_ALREADY_EXISTS"
        )
        raise HTTPException(409, detail={"reason_code": reason}) from error
    if not user:
        raise HTTPException(404, detail={"reason_code": "USER_NOT_FOUND"})
    return {key: value for key, value in user.items() if key != "password_hash"}


@router.get("/admin/accounts/{user_id}/timeline")
def admin_account_timeline(
    user_id: int,
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> list[dict[str, Any]]:
    if not repository.get_user_by_id(user_id):
        raise HTTPException(404, detail={"reason_code": "USER_NOT_FOUND"})
    return [safe_event_row(row) for row in repository.user_timeline(user_id)]


@router.get("/admin/accounts/{user_id}/biometric")
def admin_account_biometric(
    user_id: int,
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, Any]:
    if not repository.get_user_by_id(user_id):
        raise HTTPException(404, detail={"reason_code": "USER_NOT_FOUND"})
    return repository.account_biometric_summary(user_id)


@router.get("/admin/accounts/{user_id}/notes")
def get_admin_notes(
    user_id: int,
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> list[dict[str, Any]]:
    if not repository.get_user_by_id(user_id):
        raise HTTPException(404, detail={"reason_code": "USER_NOT_FOUND"})
    return repository.admin_notes(user_id)


@router.post("/admin/accounts/{user_id}/reset-password")
def admin_reset_password(
    user_id: int,
    payload: AdminPasswordResetRequest,
    admin: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
    auth: Annotated[AccountAuthService, Depends(get_account_auth_service)],
) -> dict[str, Any]:
    if not repository.get_user_by_id(user_id):
        raise HTTPException(404, detail={"reason_code": "USER_NOT_FOUND"})
    if payload.mode == "TEMPORARY":
        password = payload.temporary_password.get_secret_value() if payload.temporary_password else None
    else:
        password = payload.new_password.get_secret_value() if payload.new_password else None
    administrator_record = repository.get_user_by_id(admin.user_id) or {}
    administrator = _account_display_identity(administrator_record)
    try:
        generated = auth.admin_reset_password(
            user_id,
            password,
            must_change_password=payload.mode == "TEMPORARY",
            reason=payload.reason,
            admin_id=admin.user_id,
            administrator=str(administrator),
        )
    except AccountAuthError as error:
        raise HTTPException(422, detail={"reason_code": error.reason_code}) from error
    return {"reset": True, "temporary_password": generated}


@router.put("/admin/accounts/{user_id}/force-password-change")
def force_password_change(
    user_id: int,
    payload: ForcePasswordChangeRequest,
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, bool]:
    if not repository.get_user_by_id(user_id):
        raise HTTPException(404, detail={"reason_code": "USER_NOT_FOUND"})
    repository.set_force_password_change(user_id, payload.enabled)
    return {"enabled": payload.enabled}


@router.get("/admin/accounts/{user_id}/sessions")
def admin_user_sessions(
    user_id: int,
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> list[dict[str, Any]]:
    return repository.list_account_sessions(user_id)


@router.delete("/admin/accounts/{user_id}/sessions/{session_id}")
def admin_revoke_session(
    user_id: int,
    session_id: str,
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, bool]:
    if not repository.revoke_session(session_id, "ADMIN_REVOKED", expected_user_id=user_id):
        raise HTTPException(404, detail={"reason_code": "SESSION_NOT_FOUND"})
    repository.add_security_event("SESSION_REVOKED", "INFO", user_id, {"session_id": session_id})
    return {"revoked": True}


@router.delete("/admin/accounts/{user_id}/sessions")
def admin_revoke_all_sessions(
    user_id: int,
    payload: AdminReasonRequest,
    admin: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, int]:
    count = repository.revoke_user_sessions(user_id, "ADMIN_REVOKED_ALL")
    repository.add_audit(
        "SESSION_REVOKED",
        user_id,
        "SUCCESS",
        {"count": count, "reason": payload.reason, "admin_id": admin.user_id},
    )
    return {"revoked": count}


@router.get("/admin/accounts/{user_id}/access-events")
def admin_user_access_events(
    user_id: int,
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    return repository.access_events(user_id=user_id, limit=limit, offset=offset)


@router.get("/admin/accounts/{user_id}/security-events")
def admin_user_security_events(
    user_id: int,
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> list[dict[str, Any]]:
    return [row for row in repository.security_events(limit=500) if row.get("user_id") == user_id]


@router.delete("/admin/accounts/{user_id}/biometric")
def admin_remove_biometric(
    user_id: int,
    payload: AdminReasonRequest,
    admin: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, bool]:
    user = repository.get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, detail={"reason_code": "USER_NOT_FOUND"})
    deleted = repository.delete_biometric(str(user["external_id"]))
    repository.add_audit(
        "BIOMETRIC_REMOVED_BY_ADMIN", user_id, "SUCCESS", {"reason": payload.reason, "admin_id": admin.user_id}
    )
    return {"removed": deleted}


@router.get("/admin/dashboard")
def v3_dashboard(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, Any]:
    return {"metrics": repository.v3_admin_stats(), "recent_access": repository.access_events(limit=20)}


@router.get("/admin/system-health")
def system_health(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    pipeline: Annotated[BiometricPipeline, Depends(get_pipeline)],
) -> dict[str, Any]:
    with repository.db.connect() as conn:
        database_ok = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        storage = conn.execute(
            """SELECT storage_state,COUNT(*) AS files,COALESCE(SUM(size_bytes),0) AS size_bytes,
               MIN(created_at) AS oldest,MAX(created_at) AS newest
               FROM evidence_photos GROUP BY storage_state"""
        ).fetchall()
        last_backup = conn.execute(
            "SELECT created_at,validation_status FROM backups WHERE kind='USER' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    storage_rows = {row["storage_state"]: dict(row) for row in storage}
    return {
        "components": {
            "database": "OK" if database_ok else "ERROR",
            "yunet": "READY" if getattr(pipeline, "detector", None) else "ERROR",
            "sface": "READY" if getattr(pipeline, "embedder", None) else "ERROR",
            "mediapipe": "READY" if getattr(pipeline, "eye_geometry", None) else "ERROR",
            "active_storage": "OK" if settings.active_image_dir.parent.exists() else "WARNING",
            "archive": "OK" if settings.archive_dir.parent.exists() else "WARNING",
        },
        "storage": {
            "active": storage_rows.get("ACTIVE", {"files": 0, "size_bytes": 0, "oldest": None, "newest": None}),
            "archive": storage_rows.get("ARCHIVED", {"files": 0, "size_bytes": 0, "oldest": None, "newest": None}),
        },
        "last_backup": dict(last_backup) if last_backup else None,
    }


@router.get("/admin/settings")
def v3_settings(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict[str, Any]:
    result = {name: getattr(settings, name) for name in V3_RUNTIME_SETTINGS}
    result.update({"require_liveness": True, "active_image_days": 60, "permanent_archive": True})
    return result


@router.put("/admin/settings")
def update_v3_settings(
    payload: AdminSettingsRequest,
    admin: Annotated[AccountSession, Depends(require_account_admin)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, Any]:
    changes = payload.model_dump(exclude_none=True)
    target_face = bool(changes.get("allow_face_login", settings.allow_face_login))
    target_password = bool(changes.get("allow_password_login", settings.allow_password_login))
    if not target_face and not target_password:
        raise HTTPException(409, detail={"reason_code": "GLOBAL_AUTH_METHOD_REQUIRED"})
    repository.set_runtime_settings({f"v3_{name}": json.dumps(value) for name, value in changes.items()}, admin.user_id)
    for name, value in changes.items():
        setattr(settings, name, value)
    return v3_settings(admin, settings)


@router.get("/admin/access-events")
def all_access_events(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: str | None = None,
    method: str | None = Query(default=None, pattern=r"^(FACE|PASSWORD)$"),
    result: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    return repository.access_events(
        limit=limit,
        offset=offset,
        search=user,
        method=method,
        result=result,
        date_from=date_from,
        date_to=date_to,
    )


@router.get("/admin/access-events.csv")
def export_access_events(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
    user: str | None = None,
    method: str | None = Query(default=None, pattern=r"^(FACE|PASSWORD)$"),
    result: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> Response:
    output = io.StringIO()
    columns = [
        "timestamp",
        "user_display_name",
        "login",
        "role",
        "method",
        "result",
        "reason",
        "similarity",
        "liveness",
        "client_address",
        "user_agent",
    ]
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    rows = repository.access_events(
        limit=10000,
        search=user,
        method=method,
        result=result,
        date_from=date_from,
        date_to=date_to,
    )
    writer.writerows(
        {
            **row,
            "user_display_name": _account_display_identity(row),
            "login": row.get("login") or "",
            "role": row.get("role") or "",
            "liveness": row.get("liveness_score"),
        }
        for row in rows
    )
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=access-events.csv"},
    )


@router.get("/admin/security-events")
def all_security_events(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    rows = repository.security_events(limit, offset)
    context = repository.security_events(1000, 0)
    return [safe_event_row(row) for row in annotate_risk(rows, context)]


@router.get("/admin/unknown-faces")
def unknown_faces(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    return [safe_event_row(row) for row in repository.unknown_faces(limit, offset)]


@router.get("/admin/events/{category}/{event_id}")
def admin_event_details(
    category: str,
    event_id: int,
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, Any]:
    queries = {
        "access": """SELECT ae.*,u.external_id,u.login,u.full_name,u.role,p.id AS photo_id FROM access_events ae
                     LEFT JOIN users u ON u.id=ae.user_id LEFT JOIN evidence_photos p ON p.access_event_id=ae.id
                     WHERE ae.id=?""",
        "unknown": """SELECT ae.*,p.id AS photo_id,se.severity,se.metadata,se.reviewed_at,se.note
                      FROM access_events ae LEFT JOIN evidence_photos p ON p.access_event_id=ae.id
                      LEFT JOIN security_events se ON se.access_event_id=ae.id WHERE ae.id=?""",
        "security": """SELECT se.*,u.full_name,u.login,u.role,u.external_id FROM security_events se
                       LEFT JOIN users u ON u.id=se.user_id WHERE se.id=?""",
        "audit": """SELECT ae.*,u.full_name,u.login,u.role,u.external_id FROM audit_events ae
                    LEFT JOIN users u ON u.id=ae.user_id WHERE ae.id=?""",
    }
    query = queries.get(category)
    if not query:
        raise HTTPException(404, detail={"reason_code": "EVENT_NOT_FOUND"})
    with repository.db.connect() as conn:
        row = conn.execute(query, (event_id,)).fetchone()
    if not row:
        raise HTTPException(404, detail={"reason_code": "EVENT_NOT_FOUND"})
    result = dict(row)
    if category == "security":
        result = annotate_risk([result], repository.security_events(1000, 0))[0]
    action = repository.password_reset_action_metadata(result.get("metadata"), result.get("user_id"))
    if action:
        result["action_metadata"] = action
    return safe_event_row(result)


@router.post("/admin/security-events/{event_id}/review")
def review_security_event(
    event_id: int,
    payload: SecurityReviewRequest,
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, bool]:
    if not repository.review_security_event(event_id, payload.note):
        raise HTTPException(404, detail={"reason_code": "SECURITY_EVENT_NOT_FOUND"})
    return {"reviewed": True}


@router.get("/admin/biometric-requests")
def all_biometric_requests(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
    status: str | None = None,
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    return repository.biometric_requests(status, limit, offset)


@router.get("/admin/biometric-requests/{request_id}")
def biometric_request_detail(
    request_id: str,
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, Any]:
    detail = repository.biometric_request_detail(request_id)
    if not detail:
        raise HTTPException(404, detail={"reason_code": "BIOMETRIC_REQUEST_NOT_FOUND"})
    return detail


@router.post("/admin/biometric-requests/{request_id}/review")
def review_biometric_request(
    request_id: str,
    payload: BiometricReviewRequest,
    admin: Annotated[AccountSession, Depends(require_account_admin)],
    workflow: Annotated[BiometricWorkflowService, Depends(get_biometric_workflow)],
) -> dict[str, bool]:
    try:
        workflow.review(request_id, payload.decision, admin.user_id, payload.comment)
    except BiometricWorkflowError as error:
        raise HTTPException(409, detail={"reason_code": error.reason_code}) from error
    return {"updated": True}


@router.get("/admin/archive")
def archive_list(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
    storage_state: str | None = None,
    user_id: int | None = None,
    source: str | None = None,
    integrity: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    return [
        row
        for row in repository.evidence_photos(storage_state, limit, offset)
        if (user_id is None or row["user_id"] == user_id)
        and (not source or row["source_type"] == source)
        and (not integrity or row["integrity_status"] == integrity)
        and (not date_from or str(row["created_at"])[:10] >= date_from)
        and (not date_to or str(row["created_at"])[:10] <= date_to)
    ]


def _evidence_response(
    photo_id: str,
    session: AccountSession,
    repository: Repository,
    evidence: EvidenceStorage,
    *,
    download: bool,
) -> FileResponse:
    record = repository.get_evidence_photo(photo_id)
    if not record or (session.role != "ADMIN" and record["user_id"] != session.user_id):
        raise HTTPException(404, detail={"reason_code": "PHOTO_NOT_FOUND"})
    try:
        path = evidence.resolve(record)
    except FileNotFoundError as error:
        raise HTTPException(404, detail={"reason_code": "PHOTO_NOT_FOUND"}) from error
    if record.get("integrity_status") == "INTEGRITY_FAILED" or not evidence.verify_integrity(photo_id):
        raise HTTPException(409, detail={"reason_code": "EVIDENCE_INTEGRITY_FAILED"})
    suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(
        str(record["mime_type"]), ".bin"
    )
    date_part = str(record["created_at"])[:10].replace("-", "")
    safe_name = f"biogate_evidence_{date_part}_{photo_id[:12]}{suffix}"
    if download:
        repository.add_audit(
            "EVIDENCE_DOWNLOADED",
            record.get("user_id"),
            "SUCCESS",
            {"photo_id": photo_id, "downloaded_by": session.user_id},
        )
    return FileResponse(
        path,
        media_type=str(record["mime_type"]),
        filename=safe_name,
        content_disposition_type="attachment" if download else "inline",
        headers={"Cache-Control": "private, no-store, max-age=0", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/photos/{photo_id}/download", response_class=FileResponse)
def download_protected_photo(
    photo_id: str,
    session: Annotated[AccountSession, Depends(require_full_account_session)],
    repository: Annotated[Repository, Depends(get_repository)],
    evidence: Annotated[EvidenceStorage, Depends(get_evidence_storage)],
) -> FileResponse:
    return _evidence_response(photo_id, session, repository, evidence, download=True)


@router.get("/photos/{photo_id}", response_class=FileResponse)
def preview_protected_photo(
    photo_id: str,
    session: Annotated[AccountSession, Depends(require_full_account_session)],
    repository: Annotated[Repository, Depends(get_repository)],
    evidence: Annotated[EvidenceStorage, Depends(get_evidence_storage)],
) -> FileResponse:
    return _evidence_response(photo_id, session, repository, evidence, download=False)


@router.post("/admin/archive/run")
def run_archive(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    evidence: Annotated[EvidenceStorage, Depends(get_evidence_storage)],
) -> dict[str, int]:
    return {"archived": evidence.archive_due()}


@router.post("/admin/archive/{photo_id}/verify")
def verify_photo(
    photo_id: str,
    _: Annotated[AccountSession, Depends(require_account_admin)],
    evidence: Annotated[EvidenceStorage, Depends(get_evidence_storage)],
) -> dict[str, bool]:
    try:
        return {"valid": evidence.verify_integrity(photo_id)}
    except FileNotFoundError as error:
        raise HTTPException(404, detail={"reason_code": "PHOTO_NOT_FOUND"}) from error


@router.post("/admin/backups")
def create_backup(
    admin: Annotated[AccountSession, Depends(require_account_admin)],
    backup: Annotated[BackupService, Depends(get_backup_service)],
) -> dict[str, Any]:
    result = backup.create()
    result["created_by"] = admin.user_id
    return result


@router.get("/admin/backups")
def list_backups(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    backup: Annotated[BackupService, Depends(get_backup_service)],
) -> list[dict[str, Any]]:
    return backup.list()


@router.get("/admin/backups/{backup_id}/validate")
def validate_backup(
    backup_id: str,
    _: Annotated[AccountSession, Depends(require_account_admin)],
    backup: Annotated[BackupService, Depends(get_backup_service)],
) -> dict[str, bool]:
    return {"valid": backup.validate(backup_id)}


@router.post("/admin/backups/{backup_id}/restore")
def restore_backup(
    backup_id: str,
    _: BackupRestoreRequest,
    __: Annotated[AccountSession, Depends(require_account_admin)],
    backup: Annotated[BackupService, Depends(get_backup_service)],
) -> dict[str, Any]:
    try:
        return backup.restore(backup_id)
    except ValueError as error:
        raise HTTPException(409, detail={"reason_code": str(error)}) from error


@router.delete("/admin/backups/{backup_id}")
def delete_backup(
    backup_id: str,
    payload: AdminReasonRequest,
    admin: Annotated[AccountSession, Depends(require_account_admin)],
    backup: Annotated[BackupService, Depends(get_backup_service)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, bool]:
    if not backup.delete(backup_id):
        raise HTTPException(404, detail={"reason_code": "BACKUP_NOT_FOUND"})
    repository.add_audit(
        "BACKUP_DELETED", None, "SUCCESS", {"backup_id": backup_id, "reason": payload.reason, "admin_id": admin.user_id}
    )
    return {"deleted": True}


@router.post("/admin/accounts/{user_id}/notes")
def add_admin_note(
    user_id: int,
    payload: AdminNoteRequest,
    admin: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, Any]:
    if not repository.get_user_by_id(user_id):
        raise HTTPException(404, detail={"reason_code": "USER_NOT_FOUND"})
    with repository.db.write() as conn:
        cursor = conn.execute(
            "INSERT INTO admin_notes(user_id,author_user_id,text,created_at) VALUES(?,?,?,datetime('now'))",
            (user_id, admin.user_id, payload.text),
        )
    repository.add_audit("ADMIN_NOTE_ADDED", user_id, "SUCCESS", {"note_id": cursor.lastrowid})
    return {"id": cursor.lastrowid, "created": True}


@router.get("/admin/live")
async def live_monitor(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> StreamingResponse:
    async def stream() -> Any:
        last_id = 0
        while True:
            events = repository.access_events(limit=50)
            fresh = [event for event in reversed(events) if int(event["id"]) > last_id]
            for event in fresh:
                last_id = max(last_id, int(event["id"]))
                yield f"event: access\ndata: {json.dumps(event, default=str)}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(stream(), media_type="text/event-stream")
