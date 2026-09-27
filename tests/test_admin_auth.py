from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient
from httpx import Response

from app.core.config import Settings
from app.main import create_app
from app.services.auth import ADMIN_COOKIE_NAME
from app.services.biometrics.embedding import serialize_embedding
from tests.conftest import (
    TEST_ADMIN_HASH,
    TEST_ADMIN_NAME,
    TEST_AUTH_INPUT,
    TEST_SESSION_KEY,
    FakePipeline,
)


@contextmanager
def unauthenticated_client(
    tmp_path: Path,
    *,
    max_attempts: int = 5,
    cookie_secure: bool = False,
) -> Iterator[TestClient]:
    settings = Settings(
        db_path=tmp_path / "auth.db",
        model_cache=tmp_path / "models",
        attempt_image_dir=tmp_path / "snapshots",
        admin_username=TEST_ADMIN_NAME,
        admin_password_hash=TEST_ADMIN_HASH,
        session_secret=TEST_SESSION_KEY,
        admin_login_max_attempts=max_attempts,
        admin_login_cooldown_seconds=10,
        admin_cookie_secure=cookie_secure,
    )
    with TestClient(create_app(settings, FakePipeline()), follow_redirects=False) as test_client:  # type: ignore[arg-type]
        yield test_client


def login(client: TestClient) -> Response:
    return client.post(
        "/api/admin/auth/login",
        json={"username": TEST_ADMIN_NAME, "password": TEST_AUTH_INPUT},
    )


def test_admin_page_redirects_and_login_page_is_public(tmp_path: Path) -> None:
    with unauthenticated_client(tmp_path) as client:
        response = client.get("/admin")
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login"
        assert client.get("/admin/login").status_code == 200


def test_correct_login_cookie_and_auth_me(tmp_path: Path) -> None:
    with unauthenticated_client(tmp_path) as client:
        response = login(client)
        assert response.status_code == 200
        body = response.json()
        assert body["authenticated"] is True and body["username"] == TEST_ADMIN_NAME
        assert body["csrf_token"]
        cookie = response.headers["set-cookie"].lower()
        assert f"{ADMIN_COOKIE_NAME}=" in cookie
        assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
        me = client.get("/api/admin/auth/me")
        assert me.status_code == 200 and me.json() == body
        assert client.get("/admin").status_code == 200


def test_secure_cookie_is_configurable(tmp_path: Path) -> None:
    with unauthenticated_client(tmp_path, cookie_secure=True) as client:
        assert "secure" in login(client).headers["set-cookie"].lower()


def test_invalid_username_and_password_have_same_generic_error(tmp_path: Path) -> None:
    with unauthenticated_client(tmp_path) as client:
        wrong_username = client.post(
            "/api/admin/auth/login",
            json={"username": "not-the-admin", "password": TEST_AUTH_INPUT},
        )
        wrong_password = client.post(
            "/api/admin/auth/login",
            json={"username": TEST_ADMIN_NAME, "password": "incorrect-auth-input"},
        )
        assert wrong_username.status_code == wrong_password.status_code == 401
        assert wrong_username.json() == wrong_password.json() == {
            "detail": {"reason_code": "INVALID_ADMIN_CREDENTIALS"}
        }


def test_malformed_password_is_rejected_without_echoing_input(tmp_path: Path) -> None:
    with unauthenticated_client(tmp_path) as client:
        response = client.post(
            "/api/admin/auth/login",
            json={"username": TEST_ADMIN_NAME, "password": 123456},
        )
        assert response.status_code == 401
        assert "123456" not in response.text
        assert "password" not in response.text.lower()


def test_protected_admin_api_requires_session_and_accepts_authenticated_get(tmp_path: Path) -> None:
    with unauthenticated_client(tmp_path) as client:
        unauthorized = client.get("/api/admin/dashboard")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["detail"]["reason_code"] == "ADMIN_AUTH_REQUIRED"
        assert login(client).status_code == 200
        assert client.get("/api/admin/dashboard").status_code == 200


def test_logout_invalidates_session(tmp_path: Path) -> None:
    with unauthenticated_client(tmp_path) as client:
        signed_in = login(client).json()
        logout = client.post("/api/admin/auth/logout", headers={"X-CSRF-Token": signed_in["csrf_token"]})
        assert logout.status_code == 204
        assert client.get("/api/admin/auth/me").status_code == 401
        event_types = [event["event_type"] for event in client.app.state.repository.audit_events()]
        assert "ADMIN_LOGIN_SUCCESS" in event_types
        assert "ADMIN_LOGOUT" in event_types


def test_expired_session_requires_login_again(tmp_path: Path) -> None:
    with unauthenticated_client(tmp_path) as client:
        assert login(client).status_code == 200
        cookie = client.cookies.get(ADMIN_COOKIE_NAME)
        assert cookie
        client.app.state.auth_service.expire_for_test(cookie)
        assert client.get("/api/admin/auth/me").status_code == 401


def test_csrf_missing_invalid_and_valid(tmp_path: Path) -> None:
    with unauthenticated_client(tmp_path) as client:
        csrf_token = login(client).json()["csrf_token"]
        payload = {"external_id": "csrf-user", "display_name": "CSRF User"}
        missing = client.post("/api/admin/users", json=payload)
        invalid = client.post("/api/admin/users", json=payload, headers={"X-CSRF-Token": "invalid"})
        valid = client.post("/api/admin/users", json=payload, headers={"X-CSRF-Token": csrf_token})
        assert missing.status_code == invalid.status_code == 403
        assert missing.json()["detail"]["reason_code"] == "CSRF_VALIDATION_FAILED"
        assert invalid.json()["detail"]["reason_code"] == "CSRF_VALIDATION_FAILED"
        assert valid.status_code == 201


def test_login_brute_force_cooldown(tmp_path: Path) -> None:
    with unauthenticated_client(tmp_path, max_attempts=2) as client:
        for _ in range(2):
            response = client.post(
                "/api/admin/auth/login",
                json={"username": TEST_ADMIN_NAME, "password": "incorrect-auth-input"},
            )
            assert response.status_code == 401
        blocked = login(client)
        assert blocked.status_code == 429
        assert blocked.json()["detail"]["reason_code"] == "ADMIN_LOGIN_RATE_LIMITED"


def test_secrets_are_not_returned(tmp_path: Path) -> None:
    with unauthenticated_client(tmp_path) as client:
        response = login(client)
        combined = response.text + client.get("/api/admin/auth/me").text
        assert TEST_AUTH_INPUT not in combined
        assert TEST_ADMIN_HASH not in combined
        assert TEST_SESSION_KEY not in combined


def test_public_verification_remains_available_without_admin_session(tmp_path: Path) -> None:
    with unauthenticated_client(tmp_path) as client:
        repository = client.app.state.repository
        repository.create_user("public-user", "Public User")
        template = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
        repository.upsert_template(
            "public-user",
            "TestEmbedding",
            "1",
            serialize_embedding(template),
            int(template.size),
            3,
        )
        files = [("frames", (f"frame-{index}.jpg", b"face", "image/jpeg")) for index in range(3)]
        assert client.post("/api/verify", data={"external_id": "public-user"}, files=files).status_code == 200
        assert client.post("/api/verification-sessions", json={"external_id": "public-user"}).status_code == 201


def test_legacy_admin_operations_cannot_bypass_authentication(tmp_path: Path) -> None:
    with unauthenticated_client(tmp_path) as client:
        files = [("frames", (f"frame-{index}.jpg", b"face", "image/jpeg")) for index in range(3)]
        assert client.post(
            "/api/enroll",
            data={"external_id": "protected", "display_name": "Protected"},
            files=files,
        ).status_code == 401
        assert client.get("/api/verifications").status_code == 401
        assert client.get("/api/stats").status_code == 401
        assert client.delete("/api/users/protected/biometric").status_code == 401
