"""Local-only UI harness: python -m tests.ui.qa_server. Never imported by app.

Serves real templates with an injected test camera/response adapter. All database
and evidence paths live in TemporaryDirectory; no .env credentials are loaded.
"""

import secrets
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
from fastapi import Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import SecretStr

from app.core.config import Settings
from app.main import create_app
from app.services.account_auth import ACCOUNT_COOKIE_NAME
from tests.conftest import FakePipeline


def serve() -> None:
    with TemporaryDirectory(prefix="biogate-guidance-") as directory:
        root = Path(directory)
        settings = Settings(
            _env_file=None, db_path=root / "qa.db", model_cache=root / "models",
            active_image_dir=root / "active", archive_dir=root / "archive",
            backup_dir=root / "backups", attempt_image_dir=root / "snapshots",
            admin_username="", admin_password_hash=SecretStr(""),
            session_secret=SecretStr(secrets.token_urlsafe(48)),
        )
        app = create_app(settings, FakePipeline())  # type: ignore[arg-type]

        @app.get("/__qa/harness.js")
        def harness() -> FileResponse:
            return FileResponse(Path(__file__).with_name("harness.js"), media_type="text/javascript")

        @app.get("/__qa/session/{role}")
        def session(role: str) -> RedirectResponse:
            selected = "ADMIN" if role == "admin" else "USER"
            name = "qa-" + selected.lower()
            repository = app.state.repository
            user = repository.get_user_by_login(name) or repository.create_account(
                {"external_id": name, "login": name, "full_name": "UI Test " + selected, "role": selected}
            )
            _, cookie = app.state.account_auth_service.create_session(user, "PASSWORD", "127.0.0.1", "UI test")
            response = RedirectResponse("/admin" if selected == "ADMIN" else "/user", status_code=303)
            response.set_cookie(ACCOUNT_COOKIE_NAME, cookie, httponly=True, samesite="strict")
            return response

        @app.middleware("http")
        async def inject(request: Request, call_next):  # type: ignore[no-untyped-def]
            response = await call_next(request)
            if "text/html" not in response.headers.get("content-type", ""):
                return response
            chunks = [chunk async for chunk in response.body_iterator]
            text = b"".join(chunks).decode()
            # Adapter runs before production scripts, not through browser evaluation.
            text = text.replace("</head>", '<script src="/__qa/harness.js"></script></head>')
            headers = dict(response.headers)
            headers.pop("content-length", None)
            return HTMLResponse(text, response.status_code, headers=headers)

        uvicorn.run(app, host="127.0.0.1", port=8765)


if __name__ == "__main__":
    serve()
