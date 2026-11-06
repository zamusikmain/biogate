"""The console's /api/admin/* endpoints accept only an ADMIN account session.

The separate V2 administrator login and the public V2 verification API were removed.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from tests.conftest import TEST_ADMIN_HASH, TEST_ADMIN_NAME, TEST_AUTH_INPUT, TEST_SESSION_KEY, FakePipeline


@contextmanager
def unauthenticated_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        db_path=tmp_path / "auth.db",
        model_cache=tmp_path / "models",
        attempt_image_dir=tmp_path / "snapshots",
        active_image_dir=tmp_path / "active-images",
        archive_dir=tmp_path / "archive",
        backup_dir=tmp_path / "backups",
        admin_username=TEST_ADMIN_NAME,
        admin_password_hash=TEST_ADMIN_HASH,
        session_secret=TEST_SESSION_KEY,
    )
    with TestClient(create_app(settings, FakePipeline()), follow_redirects=False) as test_client:  # type: ignore[arg-type]
        yield test_client


def sign_in(client: TestClient, login: str = TEST_ADMIN_NAME, password: str = TEST_AUTH_INPUT) -> str:
    response = client.post("/api/v3/auth/password", json={"login": login, "password": password})
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def test_legacy_admin_login_page_redirects_to_unified_entry(tmp_path: Path) -> None:
    with unauthenticated_client(tmp_path) as client:
        response = client.get("/admin/login")
        assert response.status_code == 308
        assert response.headers["location"] == "/"
        admin = client.get("/admin")
        assert admin.status_code == 303 and admin.headers["location"] == "/"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/admin/auth/login"),
        ("GET", "/api/admin/auth/me"),
        ("POST", "/api/admin/auth/logout"),
        ("POST", "/api/enroll"),
        ("POST", "/api/verify"),
        ("GET", "/api/users"),
        ("GET", "/api/users/alice"),
        ("DELETE", "/api/users/alice/biometric"),
        ("GET", "/api/verifications"),
        ("GET", "/api/stats"),
        ("POST", "/api/verification-sessions"),
        ("POST", "/api/verification-sessions/some-id/frames"),
        ("DELETE", "/api/verification-sessions/some-id"),
    ],
)
def test_removed_v2_endpoints_are_gone(tmp_path: Path, method: str, path: str) -> None:
    with unauthenticated_client(tmp_path) as client:
        assert client.request(method, path).status_code in {404, 405}


def test_admin_api_requires_an_account_session(tmp_path: Path) -> None:
    with unauthenticated_client(tmp_path) as client:
        unauthorized = client.get("/api/admin/dashboard")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["detail"]["reason_code"] == "ADMIN_AUTH_REQUIRED"
        sign_in(client)
        assert client.get("/api/admin/dashboard").status_code == 200


def test_admin_api_rejects_user_accounts(tmp_path: Path) -> None:
    with unauthenticated_client(tmp_path) as client:
        csrf = sign_in(client)
        created = client.post(
            "/api/v3/admin/accounts",
            headers={"X-CSRF-Token": csrf},
            json={
                "external_id": "plain-user",
                "login": "plain-user",
                "full_name": "Plain",
                "password_mode": "PERMANENT",
                "new_password": "Plain-user-pass-1!",
                "confirmation": "Plain-user-pass-1!",
            },
        )
        assert created.status_code == 201
        client.cookies.clear()
        sign_in(client, "plain-user", "Plain-user-pass-1!")
        forbidden = client.get("/api/admin/dashboard")
        assert forbidden.status_code == 403
        assert forbidden.json()["detail"]["reason_code"] == "ADMIN_REQUIRED"


def test_admin_api_requires_csrf_for_writes(tmp_path: Path) -> None:
    with unauthenticated_client(tmp_path) as client:
        csrf = sign_in(client)
        payload = {"external_id": "csrf-user", "display_name": "CSRF User"}
        missing = client.post("/api/admin/users", json=payload)
        invalid = client.post("/api/admin/users", json=payload, headers={"X-CSRF-Token": "invalid"})
        valid = client.post("/api/admin/users", json=payload, headers={"X-CSRF-Token": csrf})
        assert missing.status_code == invalid.status_code == 403
        assert missing.json()["detail"]["reason_code"] == "CSRF_VALIDATION_FAILED"
        assert invalid.json()["detail"]["reason_code"] == "CSRF_VALIDATION_FAILED"
        assert valid.status_code == 201


def test_admin_api_rejects_session_that_must_change_password(tmp_path: Path) -> None:
    with unauthenticated_client(tmp_path) as client:
        csrf = sign_in(client)
        created = client.post(
            "/api/v3/admin/accounts",
            headers={"X-CSRF-Token": csrf},
            json={
                "external_id": "new-admin",
                "login": "new-admin",
                "full_name": "New Admin",
                "role": "ADMIN",
                "temporary_password": "Temporary-pass-123",
            },
        )
        assert created.status_code == 201
        client.cookies.clear()
        sign_in(client, "new-admin", "Temporary-pass-123")
        response = client.get("/api/admin/dashboard")
        assert response.status_code == 403
        assert response.json()["detail"]["reason_code"] == "PASSWORD_CHANGE_REQUIRED"


def test_logout_ends_admin_api_access(tmp_path: Path) -> None:
    with unauthenticated_client(tmp_path) as client:
        csrf = sign_in(client)
        assert client.post("/api/v3/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
        assert client.get("/api/admin/dashboard").status_code == 401
