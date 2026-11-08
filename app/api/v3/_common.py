"""Helpers and settings shared by the V3 routers."""

import logging
from typing import Any

from fastapi import Request, Response

from app.core.config import Settings
from app.services.account_auth import ACCOUNT_COOKIE_NAME, AccountSession

logger = logging.getLogger("biogate.liveness")


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
