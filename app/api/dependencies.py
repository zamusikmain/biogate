import secrets
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request

from app.core.config import Settings
from app.services.account_auth import ACCOUNT_COOKIE_NAME, AccountAuthService, AccountSession
from app.services.auth import ADMIN_COOKIE_NAME, AdminAuthService, AdminSession
from app.services.biometric_workflow import BiometricWorkflowService
from app.services.biometrics.pipeline import BiometricPipeline
from app.services.challenges import ChallengeService
from app.services.face_login import FaceLoginService
from app.services.identification import IdentificationService
from app.services.repository import Repository
from app.services.storage import BackupService, EvidenceStorage


def get_repository(request: Request) -> Repository:
    return cast(Repository, request.app.state.repository)


def get_pipeline(request: Request) -> BiometricPipeline:
    return cast(BiometricPipeline, request.app.state.pipeline)


def get_app_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_challenge_service(request: Request) -> ChallengeService:
    return cast(ChallengeService, request.app.state.challenge_service)


def get_auth_service(request: Request) -> AdminAuthService:
    return cast(AdminAuthService, request.app.state.auth_service)


def get_account_auth_service(request: Request) -> AccountAuthService:
    return cast(AccountAuthService, request.app.state.account_auth_service)


def get_identification_service(request: Request) -> IdentificationService:
    return cast(IdentificationService, request.app.state.identification_service)


def get_evidence_storage(request: Request) -> EvidenceStorage:
    return cast(EvidenceStorage, request.app.state.evidence_storage)


def get_backup_service(request: Request) -> BackupService:
    return cast(BackupService, request.app.state.backup_service)


def get_biometric_workflow(request: Request) -> BiometricWorkflowService:
    return cast(BiometricWorkflowService, request.app.state.biometric_workflow)


def get_face_login_service(request: Request) -> FaceLoginService:
    return cast(FaceLoginService, request.app.state.face_login_service)


def require_account_session(
    request: Request,
    auth: Annotated[AccountAuthService, Depends(get_account_auth_service)],
) -> AccountSession:
    session = auth.get_session(request.cookies.get(ACCOUNT_COOKIE_NAME))
    if not session:
        raise HTTPException(401, detail={"reason_code": "AUTH_REQUIRED"})
    return session


def require_account_csrf(
    request: Request,
    session: Annotated[AccountSession, Depends(require_account_session)],
) -> AccountSession:
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not (
        request.headers.get("X-CSRF-Token")
        and secrets.compare_digest(request.headers["X-CSRF-Token"], session.csrf_token)
    ):
        raise HTTPException(403, detail={"reason_code": "CSRF_VALIDATION_FAILED"})
    return session


def require_full_account_session(
    session: Annotated[AccountSession, Depends(require_account_csrf)],
) -> AccountSession:
    if session.must_change_password:
        raise HTTPException(403, detail={"reason_code": "PASSWORD_CHANGE_REQUIRED"})
    return session


def require_account_admin(
    session: Annotated[AccountSession, Depends(require_full_account_session)],
) -> AccountSession:
    if session.role != "ADMIN":
        raise HTTPException(403, detail={"reason_code": "ADMIN_REQUIRED"})
    return session


def require_admin_session(
    request: Request,
    auth: Annotated[AdminAuthService, Depends(get_auth_service)],
    account_auth: Annotated[AccountAuthService, Depends(get_account_auth_service)],
) -> AdminSession | AccountSession:
    account_session = account_auth.get_session(request.cookies.get(ACCOUNT_COOKIE_NAME))
    if account_session and account_session.role == "ADMIN":
        if account_session.must_change_password:
            raise HTTPException(403, detail={"reason_code": "PASSWORD_CHANGE_REQUIRED"})
        return account_session
    session = auth.get_session(request.cookies.get(ADMIN_COOKIE_NAME))
    if not session:
        raise HTTPException(401, detail={"reason_code": "ADMIN_AUTH_REQUIRED"})
    return session


def require_admin_csrf(
    request: Request,
    session: Annotated[AdminSession | AccountSession, Depends(require_admin_session)],
    auth: Annotated[AdminAuthService, Depends(get_auth_service)],
) -> None:
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    token = request.headers.get("X-CSRF-Token")
    if isinstance(session, AccountSession):
        valid = bool(token) and secrets.compare_digest(token or "", session.csrf_token)
    else:
        valid = auth.validate_csrf(session, token)
    if not valid:
        raise HTTPException(403, detail={"reason_code": "CSRF_VALIDATION_FAILED"})


RepositoryDep = Annotated[Repository, get_repository]
PipelineDep = Annotated[BiometricPipeline, get_pipeline]
