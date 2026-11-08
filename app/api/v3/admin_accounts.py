"""Administration of accounts, their sessions, notes and biometric requests."""

import secrets
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.dependencies import (
    get_account_auth_service,
    get_biometric_workflow,
    get_evidence_storage,
    get_repository,
    require_account_admin,
)
from app.api.event_safety import safe_event_row
from app.api.v3._common import _account_display_identity
from app.schemas.v3 import (
    AccountCreateRequest,
    AccountErasureRequest,
    AccountUpdateRequest,
    AdminNoteRequest,
    AdminPasswordResetRequest,
    AdminReasonRequest,
    BiometricReviewRequest,
    ForcePasswordChangeRequest,
)
from app.services.account_auth import AccountAuthError, AccountAuthService, AccountSession
from app.services.biometric_workflow import BiometricWorkflowError, BiometricWorkflowService
from app.services.repository import Repository
from app.services.storage import EvidenceStorage

router = APIRouter()


def _generate_external_id(repository: Repository) -> str:
    """Opaque, stable identifier for accounts created without one.

    It is deliberately unrelated to the login, so renaming a login never makes the
    two fields disagree in a confusing way.
    """
    while True:
        candidate = f"bg-{secrets.token_hex(6)}"
        if not repository.get_user(candidate):
            return candidate


@router.post("/admin/accounts", status_code=201)
def create_account(
    payload: AccountCreateRequest,
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
    auth: Annotated[AccountAuthService, Depends(get_account_auth_service)],
) -> dict[str, Any]:
    temporary = payload.password_mode == "TEMPORARY"  # noqa: S105 -- mode name, not a value
    generated = None
    if not temporary:
        password = payload.new_password.get_secret_value() if payload.new_password else None
    else:
        password = payload.temporary_password.get_secret_value() if payload.temporary_password else None
        if payload.generate_temporary_password:
            generated = auth.generate_temporary_password()
            password = generated
    if not password:
        raise HTTPException(422, detail={"reason_code": "TEMPORARY_PASSWORD_REQUIRED"})
    try:
        password_hash = auth.hash_password(password)
        external_id = payload.external_id or _generate_external_id(repository)
        user = repository.create_account(
            {
                **payload.model_dump(
                    exclude={
                        "password_mode",
                        "temporary_password",
                        "generate_temporary_password",
                        "new_password",
                        "confirmation",
                    }
                ),
                "external_id": external_id,
                "password_hash": password_hash,
                "must_change_password": temporary,
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
    removes_active_admin = (
        current.get("role") == "ADMIN"
        and current.get("status") == "ACTIVE"
        and (payload.role == "USER" or payload.status in {"BLOCKED", "DISABLED"})
    )
    if removes_active_admin and repository.active_admin_count(excluding_user_id=user_id) == 0:
        raise HTTPException(409, detail={"reason_code": "LAST_ACTIVE_ADMIN_REQUIRED"})
    enabled_password = (
        payload.password_enabled if payload.password_enabled is not None else bool(current.get("password_enabled", 1))
    )
    enabled_face = payload.face_enabled if payload.face_enabled is not None else bool(current.get("face_enabled", 1))
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


@router.delete("/admin/accounts/{user_id}")
def admin_erase_account(
    user_id: int,
    payload: AccountErasureRequest,
    admin: Annotated[AccountSession, Depends(require_account_admin)],
    evidence: Annotated[EvidenceStorage, Depends(get_evidence_storage)],
) -> dict[str, Any]:
    """Permanently erase a person: account, templates, samples, evidence files and identifying event data."""
    if user_id == admin.user_id:
        raise HTTPException(409, detail={"reason_code": "CANNOT_ERASE_OWN_ACCOUNT"})
    try:
        result = evidence.erase_account(user_id, admin_id=admin.user_id, reason=payload.reason)
    except ValueError as error:
        raise HTTPException(409, detail={"reason_code": str(error)}) from error
    if result is None:
        raise HTTPException(404, detail={"reason_code": "USER_NOT_FOUND"})
    return result


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
