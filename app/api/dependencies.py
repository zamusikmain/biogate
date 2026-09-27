from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request

from app.core.config import Settings
from app.services.auth import ADMIN_COOKIE_NAME, AdminAuthService, AdminSession
from app.services.biometrics.pipeline import BiometricPipeline
from app.services.challenges import ChallengeService
from app.services.repository import Repository


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


def require_admin_session(
    request: Request,
    auth: Annotated[AdminAuthService, Depends(get_auth_service)],
) -> AdminSession:
    session = auth.get_session(request.cookies.get(ADMIN_COOKIE_NAME))
    if not session:
        raise HTTPException(401, detail={"reason_code": "ADMIN_AUTH_REQUIRED"})
    return session


def require_admin_csrf(
    request: Request,
    session: Annotated[AdminSession, Depends(require_admin_session)],
    auth: Annotated[AdminAuthService, Depends(get_auth_service)],
) -> None:
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not auth.validate_csrf(
        session, request.headers.get("X-CSRF-Token")
    ):
        raise HTTPException(403, detail={"reason_code": "CSRF_VALIDATION_FAILED"})


RepositoryDep = Annotated[Repository, get_repository]
PipelineDep = Annotated[BiometricPipeline, get_pipeline]
