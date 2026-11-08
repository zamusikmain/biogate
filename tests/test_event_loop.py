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
