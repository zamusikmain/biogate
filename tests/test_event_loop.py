import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.database import Database
from app.main import create_app
from app.services.biometrics.models import FrameResult
from app.services.repository import Repository
from tests.conftest import FakePipeline, create_account


def test_slow_frame_analysis_does_not_block_other_requests(client: TestClient) -> None:
    user_id = create_account(client, "slow-frame")
    pipeline = client.app.state.pipeline  # type: ignore[attr-defined]
    original = pipeline.process_enrollment_frame
    started, release = threading.Event(), threading.Event()

    def slow(data: bytes) -> FrameResult:
        started.set()
        release.wait(10)
        result: FrameResult = original(data)
        return result

    pipeline.process_enrollment_frame = slow
    try:
        with ThreadPoolExecutor(2) as pool:
            frame = pool.submit(
                client.post,
                f"/api/v3/admin/accounts/{user_id}/enrollment/validate",
                files={"photo": ("f.jpg", b"face", "image/jpeg")},
            )
            assert started.wait(5)
            # While the analysis is still running, the server must keep answering.
            health = pool.submit(client.get, "/health/live")
            assert health.result(timeout=3).status_code == 200
            release.set()
            assert frame.result(timeout=5).json()["accepted"] is True
    finally:
        release.set()
        pipeline.process_enrollment_frame = original


def test_admin_search_finds_cyrillic_names_in_any_case(client: TestClient) -> None:
    user_id = create_account(client, "anna.s", "Анна Соколова")
    client.app.state.repository.add_access_event(  # type: ignore[attr-defined]
        {"user_id": user_id, "method": "FACE", "result": "SUCCESS", "reason": "IDENTIFIED"}
    )
    for term in ("анна", "АННА", "Соколова", "anna.S"):
        accounts = client.get("/api/v3/admin/accounts", params={"search": term}).json()
        assert [row["id"] for row in accounts] == [user_id], term
        events = client.get("/api/v3/admin/access-events", params={"user": term}).json()
        assert [row["user_id"] for row in events] == [user_id], term
    assert client.get("/api/v3/admin/accounts", params={"search": "петров"}).json() == []


def test_face_login_photos_reach_the_card_and_only_their_owner(client: TestClient) -> None:
    import cv2

    from app.services.account_auth import ACCOUNT_COOKIE_NAME

    state = client.app.state  # type: ignore[attr-defined]
    jpeg = cv2.imencode(".jpg", np.full((40, 40, 3), 128, dtype=np.uint8))[1].tobytes()

    def face_login(user_id: int) -> str:
        event_id = state.repository.add_access_event(
            {"user_id": user_id, "method": "FACE", "result": "SUCCESS", "reason": "IDENTIFIED"}
        )
        return str(
            state.evidence_storage.save(
                jpeg, "image/jpeg", "FACE_IDENTIFIED", user_id=user_id, access_event_id=event_id
            )["id"]
        )

    anna, bob = create_account(client, "anna.photo"), create_account(client, "bob.photo")
    anna_photo, bob_photo = face_login(anna), face_login(bob)
    # The admin card's access tab gets the photo id it renders as a thumbnail.
    assert client.get(f"/api/v3/admin/accounts/{anna}/access-events").json()[0]["photo_id"] == anna_photo

    state.repository.set_force_password_change(anna, False)
    _, cookie = state.account_auth_service.create_session(state.repository.get_user_by_id(anna), "PASSWORD")
    client.cookies.clear()
    client.cookies.set(ACCOUNT_COOKIE_NAME, cookie)
    own = client.get("/api/v3/user/access-events").json()
    assert [row["photo_id"] for row in own] == [anna_photo]
    assert client.get(f"/api/v3/photos/{anna_photo}").status_code == 200
    assert client.get(f"/api/v3/photos/{bob_photo}").status_code == 404


def test_unknown_login_costs_one_argon2_verification(client: TestClient) -> None:
    auth = client.app.state.account_auth_service  # type: ignore[attr-defined]
    calls: list[str] = []
    original = auth.hasher

    class CountingHasher:
        def hash(self, password: str) -> str:
            return str(original.hash(password))

        def verify(self, password_hash: str, password: str) -> bool:
            calls.append(password_hash)
            return bool(original.verify(password_hash, password))

    auth.hasher = CountingHasher()
    try:
        response = client.post("/api/v3/auth/password", json={"login": "nobody-here", "password": "Wrong-pass-123"})
    finally:
        auth.hasher = original
    assert response.json()["detail"]["reason_code"] == "INVALID_CREDENTIALS"
    assert len(calls) == 1


def test_wrong_password_does_not_reveal_a_password_disabled_account(client: TestClient) -> None:
    from argon2 import PasswordHasher, Type

    state = client.app.state  # type: ignore[attr-defined]
    user_id = create_account(client, "no.password")
    secret = "Correct-pass-123"  # noqa: S105 -- test-only credential
    state.repository.set_password_hash(user_id, PasswordHasher(type=Type.ID).hash(secret), False, "TEST")
    state.repository.update_account(user_id, {"password_enabled": False})
    client.cookies.clear()
    wrong = client.post("/api/v3/auth/password", json={"login": "no.password", "password": "Wrong-pass-1234"})
    assert wrong.status_code == 401 and wrong.json()["detail"]["reason_code"] == "INVALID_CREDENTIALS"
    right = client.post("/api/v3/auth/password", json={"login": "no.password", "password": secret})
    assert right.json()["detail"]["reason_code"] == "ACCOUNT_PASSWORD_DISABLED"


def test_password_policy_accepts_any_alphabet() -> None:
    from app.services.account_auth import AccountAuthError, AccountAuthService

    AccountAuthService.validate_password("Пароль-надёжный1")
    with pytest.raises(AccountAuthError, match="UPPERCASE"):
        AccountAuthService.validate_password("пароль-надёжный1")
    with pytest.raises(AccountAuthError, match="LOWERCASE"):
        AccountAuthService.validate_password("ПАРОЛЬ-НАДЁЖНЫЙ1")


def test_invalid_stored_runtime_setting_is_ignored(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, db_path=tmp_path / "biogate.db", **_dirs(tmp_path))  # type: ignore[call-arg]
    database = Database(settings.db_path)
    database.initialize()
    Repository(database).set_setting("v3_identification_threshold", "7.5")
    Repository(database).set_setting("v3_recovery_max_attempts", "not json")
    Repository(database).set_setting("v3_password_login_max_attempts", "9")
    with TestClient(create_app(settings, FakePipeline())):  # type: ignore[arg-type]
        assert settings.identification_threshold == 0.363
        assert settings.recovery_max_attempts == 5
        assert settings.password_login_max_attempts == 9
    with pytest.raises(ValueError):
        settings.identification_threshold = 2.0


def _dirs(tmp_path: Path) -> dict[str, Path]:
    return {
        "model_cache": tmp_path / "models",
        "attempt_image_dir": tmp_path / "snapshots",
        "active_image_dir": tmp_path / "active-images",
        "archive_dir": tmp_path / "archive",
        "backup_dir": tmp_path / "backups",
    }


def test_pipeline_serializes_model_calls() -> None:
    from app.services.biometrics.pipeline import BiometricPipeline

    pipeline = object.__new__(BiometricPipeline)
    active = 0
    peak = 0
    guard = threading.Lock()

    def detect(_: object) -> object:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        threading.Event().wait(0.05)
        with guard:
            active -= 1
        raise RuntimeError("stop after detection")

    pipeline.decode = lambda _: np.zeros((10, 10, 3), dtype=np.uint8)  # type: ignore[method-assign]
    pipeline.detector = type("Detector", (), {"detect_one": lambda self, image: detect(image)})()

    def call() -> None:
        try:
            pipeline.process_enrollment_frame(b"x")
        except RuntimeError:
            pass

    threads = [threading.Thread(target=call) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak == 1
