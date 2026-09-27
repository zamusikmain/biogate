from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.realtime import router as realtime_router
from app.api.routes import router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.database import Database
from app.services.auth import ADMIN_COOKIE_NAME, AdminAuthService
from app.services.biometrics.pipeline import BiometricPipeline
from app.services.challenges import ChallengeService
from app.services.repository import Repository

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def create_app(settings: Settings | None = None, pipeline: BiometricPipeline | None = None) -> FastAPI:
    config = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(config.debug)
        database = Database(config.db_path)
        database.initialize()
        app.state.settings = config
        app.state.repository = Repository(database)
        app.state.auth_service = AdminAuthService(config, app.state.repository)
        app.state.pipeline = pipeline or BiometricPipeline(config)
        app.state.challenge_service = ChallengeService(app.state.repository, config)
        yield

    app = FastAPI(
        title="BioGate API",
        version="0.1.0",
        description="Local 1:1 biometric face verification",
        lifespan=lifespan,
    )
    app.include_router(router)
    app.include_router(realtime_router)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request=request, name="index.html")

    @app.get("/admin/login", response_class=HTMLResponse, include_in_schema=False)
    async def admin_login(request: Request) -> Response:
        auth = request.app.state.auth_service
        if auth.get_session(request.cookies.get(ADMIN_COOKIE_NAME)):
            return RedirectResponse("/admin", status_code=303)
        return templates.TemplateResponse(request=request, name="admin_login.html")

    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/admin/users/{external_id}", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/admin/verifications/{attempt_id}", response_class=HTMLResponse, include_in_schema=False)
    async def admin(
        request: Request, external_id: str | None = None, attempt_id: int | None = None
    ) -> Response:
        auth = request.app.state.auth_service
        if not auth.get_session(request.cookies.get(ADMIN_COOKIE_NAME)):
            return RedirectResponse("/admin/login", status_code=303)
        return templates.TemplateResponse(request=request, name="admin.html")

    return app


app = create_app()
