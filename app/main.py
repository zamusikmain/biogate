import json
import logging
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import RequestResponseEndpoint

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.realtime import router as realtime_router
from app.api.routes import router
from app.api.v3 import V3_RUNTIME_SETTINGS
from app.api.v3 import router as v3_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.database import Database
from app.services.account_auth import ACCOUNT_COOKIE_NAME, AccountAuthService
from app.services.auth import ADMIN_COOKIE_NAME, AdminAuthService
from app.services.biometric_workflow import BiometricWorkflowService
from app.services.biometrics.pipeline import BiometricPipeline
from app.services.challenges import ChallengeService
from app.services.face_login import FaceLoginService
from app.services.identification import IdentificationService
from app.services.repository import Repository
from app.services.storage import BackupService, EvidenceStorage

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")
request_logger = logging.getLogger("biogate.request")


def create_app(settings: Settings | None = None, pipeline: BiometricPipeline | None = None) -> FastAPI:
    config = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(config.debug)
        database = Database(config.db_path)
        database.initialize()
        app.state.settings = config
        for directory in (
            config.active_image_dir,
            config.archive_dir,
            config.backup_dir,
            config.attempt_image_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        app.state.repository = Repository(database)
        for setting_name in V3_RUNTIME_SETTINGS:
            stored = app.state.repository.get_setting(f"v3_{setting_name}")
            if stored is not None:
                setattr(config, setting_name, json.loads(stored))
        admin_hash = config.admin_password_hash.get_secret_value()
        if config.admin_username and admin_hash:
            app.state.repository.ensure_bootstrap_admin(config.admin_username, admin_hash)
        app.state.auth_service = AdminAuthService(config, app.state.repository)
        app.state.account_auth_service = AccountAuthService(app.state.repository, config)
        app.state.identification_service = IdentificationService(app.state.repository, config)
        app.state.evidence_storage = EvidenceStorage(app.state.repository, config)
        app.state.backup_service = BackupService(app.state.repository, config)
        app.state.biometric_workflow = BiometricWorkflowService(app.state.repository, app.state.identification_service)
        app.state.pipeline = pipeline or BiometricPipeline(config)
        app.state.challenge_service = ChallengeService(app.state.repository, config, app.state.identification_service)
        app.state.face_login_service = FaceLoginService(
            app.state.repository,
            config,
            app.state.challenge_service,
            app.state.identification_service,
        )
        yield

    app = FastAPI(
        title="BioGate API",
        version="0.3.0",
        description="Local-first BioGate V3 identity and access management API.",
        lifespan=lifespan,
        openapi_tags=[
            {"name": "Authentication", "description": "Password, face and recovery authentication."},
            {"name": "Users", "description": "Authenticated USER self-service operations."},
            {"name": "Biometrics", "description": "Biometric capture and approval workflows."},
            {"name": "Access", "description": "Access decisions and security events."},
            {"name": "Evidence", "description": "Protected evidence retrieval and integrity."},
            {"name": "Sessions", "description": "Session inspection and revocation."},
            {"name": "Admin", "description": "Administrative account and system operations."},
            {"name": "Backups", "description": "Versioned local backup operations."},
            {"name": "Health", "description": "Safe process health information."},
        ],
    )
    app.include_router(router)
    app.include_router(realtime_router)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(v3_router)
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    def openapi_schema() -> dict[str, object]:
        if app.openapi_schema:
            return app.openapi_schema
        documented_routes = [
            route
            for route in app.routes
            if str(getattr(route, "path", "")).startswith("/api/v3/") or str(getattr(route, "path", "")) == "/health"
        ]
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=documented_routes,
        )
        paths = schema.get("paths", {})
        components = schema.setdefault("components", {})
        if isinstance(components, dict):
            components.setdefault("securitySchemes", {})["BioGateSession"] = {
                "type": "apiKey",
                "in": "cookie",
                "name": ACCOUNT_COOKIE_NAME,
            }
            components.setdefault("schemas", {})["ControlledError"] = {
                "type": "object",
                "properties": {
                    "detail": {
                        "type": "object",
                        "properties": {"reason_code": {"type": "string"}},
                        "required": ["reason_code"],
                    }
                },
                "required": ["detail"],
            }
        if isinstance(paths, dict):
            for path in list(paths):
                if not (path.startswith("/api/v3/") or path == "/health"):
                    paths.pop(path)
                    continue
                operations = paths[path]
                if not isinstance(operations, dict):
                    continue
                tag = _openapi_tag(path)
                for operation in operations.values():
                    if isinstance(operation, dict):
                        operation["tags"] = [tag]
                        if path.startswith("/api/v3/"):
                            if _openapi_requires_session(path):
                                operation["security"] = [{"BioGateSession": []}]
                            responses = operation.setdefault("responses", {})
                            for status_code in ("400", "401", "403", "404", "409", "422", "429"):
                                responses.setdefault(
                                    status_code,
                                    {
                                        "description": "Controlled API error",
                                        "content": {
                                            "application/json": {
                                                "schema": {"$ref": "#/components/schemas/ControlledError"}
                                            }
                                        },
                                    },
                                )
        app.openapi_schema = schema
        return schema

    app.openapi = openapi_schema  # type: ignore[method-assign]

    @app.middleware("http")
    async def request_context(request: Request, call_next: RequestResponseEndpoint) -> Response:
        supplied = request.headers.get("X-Request-ID", "")
        valid_supplied = 1 <= len(supplied) <= 64 and all(c.isalnum() or c in "-_." for c in supplied)
        request_id = supplied if valid_supplied else str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        request_logger.info(
            "HTTP request",
            extra={
                "request_id": request_id,
                "route": request.url.path,
                "method": request.method,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return response

    @app.middleware("http")
    async def security_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
            "script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
        )
        if request.url.path == "/" or request.url.path.startswith(("/api/v3", "/admin", "/user", "/static")):
            response.headers.setdefault("Cache-Control", "private, no-store, max-age=0")
        return response

    @app.exception_handler(sqlite3.OperationalError)
    async def sqlite_operational_error(_: Request, __: sqlite3.OperationalError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": {"reason_code": "DATABASE_TEMPORARILY_UNAVAILABLE"}},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request) -> Response:
        session = request.app.state.account_auth_service.get_session(request.cookies.get(ACCOUNT_COOKIE_NAME))
        if session and not session.must_change_password:
            return RedirectResponse("/admin" if session.role == "ADMIN" else "/user", status_code=303)
        return templates.TemplateResponse(request=request, name="login.html")

    @app.get("/user", response_class=HTMLResponse, include_in_schema=False)
    async def user_app(request: Request) -> Response:
        session = request.app.state.account_auth_service.get_session(request.cookies.get(ACCOUNT_COOKIE_NAME))
        if not session or session.must_change_password:
            return RedirectResponse("/", status_code=303)
        if session.role == "ADMIN":
            return RedirectResponse("/admin", status_code=303)
        return templates.TemplateResponse(request=request, name="user.html")

    @app.get("/admin/login", response_class=HTMLResponse, include_in_schema=False)
    async def admin_login(request: Request) -> Response:
        account_session = request.app.state.account_auth_service.get_session(request.cookies.get(ACCOUNT_COOKIE_NAME))
        if account_session:
            return RedirectResponse("/admin" if account_session.role == "ADMIN" else "/user", status_code=303)
        auth = request.app.state.auth_service
        if auth.get_session(request.cookies.get(ADMIN_COOKIE_NAME)):
            return templates.TemplateResponse(request=request, name="admin_login.html")
        return templates.TemplateResponse(request=request, name="admin_login.html")

    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/admin/users/{external_id}", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/admin/verifications/{attempt_id}", response_class=HTMLResponse, include_in_schema=False)
    async def admin(request: Request, external_id: str | None = None, attempt_id: int | None = None) -> Response:
        account_session = request.app.state.account_auth_service.get_session(request.cookies.get(ACCOUNT_COOKIE_NAME))
        if account_session:
            if account_session.must_change_password:
                return RedirectResponse("/", status_code=303)
            if account_session.role != "ADMIN":
                return RedirectResponse("/user", status_code=303)
            return templates.TemplateResponse(request=request, name="admin.html")
        return RedirectResponse("/", status_code=303)

    return app


def _openapi_tag(path: str) -> str:
    if path == "/health":
        return "Health"
    if "/auth/" in path or "/recovery/" in path or "/face/" in path:
        return "Authentication"
    if "/backups" in path:
        return "Backups"
    if "/photos/" in path or "/archive" in path:
        return "Evidence"
    if "/sessions" in path:
        return "Sessions"
    if "/biometric" in path:
        return "Biometrics"
    if "/access-events" in path or "/security-events" in path or "/events/" in path:
        return "Access"
    if "/user/" in path:
        return "Users"
    return "Admin"


def _openapi_requires_session(path: str) -> bool:
    public = (
        "/api/v3/auth/password",
        "/api/v3/face/challenges",
        "/api/v3/recovery/challenges",
        "/api/v3/recovery/reset",
    )
    return not any(path == candidate or path.startswith(f"{candidate}/") for candidate in public)


app = create_app()
