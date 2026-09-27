from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.api.dependencies import get_auth_service, require_admin_csrf, require_admin_session
from app.schemas.api import AdminAuthResponse
from app.services.auth import (
    ADMIN_COOKIE_NAME,
    AdminAuthService,
    AdminSession,
    AuthNotConfiguredError,
    InvalidCredentialsError,
    LoginRateLimitedError,
)

router = APIRouter(prefix="/api/admin/auth", tags=["Admin authentication"])


def _invalid_credentials() -> NoReturn:
    raise HTTPException(401, detail={"reason_code": "INVALID_ADMIN_CREDENTIALS"})


@router.post("/login", response_model=AdminAuthResponse)
async def login(
    request: Request,
    response: Response,
    auth: Annotated[AdminAuthService, Depends(get_auth_service)],
) -> AdminAuthResponse:
    if not auth.configured:
        raise HTTPException(503, detail={"reason_code": "ADMIN_AUTH_NOT_CONFIGURED"})
    try:
        payload = await request.json()
    except ValueError:
        _invalid_credentials()
    if not isinstance(payload, dict):
        _invalid_credentials()
    username = payload.get("username")
    password = payload.get("password")
    if (
        not isinstance(username, str)
        or not 1 <= len(username) <= 100
        or not isinstance(password, str)
        or not 1 <= len(password) <= 1024
    ):
        _invalid_credentials()
    client_key = request.client.host if request.client else "local"
    try:
        session, cookie_value = auth.login(username, password, client_key)
    except AuthNotConfiguredError as error:
        raise HTTPException(503, detail={"reason_code": "ADMIN_AUTH_NOT_CONFIGURED"}) from error
    except InvalidCredentialsError as error:
        raise HTTPException(401, detail={"reason_code": "INVALID_ADMIN_CREDENTIALS"}) from error
    except LoginRateLimitedError as error:
        raise HTTPException(429, detail={"reason_code": "ADMIN_LOGIN_RATE_LIMITED"}) from error
    response.set_cookie(
        key=ADMIN_COOKIE_NAME,
        value=cookie_value,
        max_age=auth.settings.admin_session_ttl_seconds,
        httponly=True,
        secure=auth.settings.admin_cookie_secure,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return AdminAuthResponse(authenticated=True, username=session.username, csrf_token=session.csrf_token)


@router.get("/me", response_model=AdminAuthResponse)
def me(
    response: Response,
    session: Annotated[AdminSession, Depends(require_admin_session)],
) -> AdminAuthResponse:
    response.headers["Cache-Control"] = "no-store"
    return AdminAuthResponse(authenticated=True, username=session.username, csrf_token=session.csrf_token)


@router.post("/logout", status_code=204, dependencies=[Depends(require_admin_csrf)])
def logout(
    request: Request,
    response: Response,
    auth: Annotated[AdminAuthService, Depends(get_auth_service)],
) -> None:
    auth.logout(request.cookies.get(ADMIN_COOKIE_NAME))
    response.delete_cookie(ADMIN_COOKIE_NAME, path="/", secure=auth.settings.admin_cookie_secure, samesite="strict")
