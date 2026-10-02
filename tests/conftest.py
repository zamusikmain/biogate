from pathlib import Path

import numpy as np
import pytest
from argon2 import PasswordHasher, Type
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.services.biometrics.errors import BiometricError
from app.services.biometrics.models import FaceObservation, FrameAnalysis, FrameResult, LivenessResult, QualityResult

TEST_ADMIN_NAME = "test-admin"
TEST_AUTH_INPUT = "test-only-auth-input"
TEST_ADMIN_HASH = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1, type=Type.ID).hash(TEST_AUTH_INPUT)
TEST_SESSION_KEY = "test-session-key-that-is-longer-than-thirty-two-bytes"


class FakePipeline:
    model_name = "TestEmbedding"
    model_version = "1"

    def create_template(self, frames: list[bytes]) -> tuple[np.ndarray, float]:
        if any(frame == b"invalid" for frame in frames):
            raise BiometricError("INVALID_IMAGE", "Invalid image")
        if any(frame == b"noface" for frame in frames):
            raise BiometricError("NO_FACE", "No face")
        if any(frame == b"multiple" for frame in frames):
            raise BiometricError("MULTIPLE_FACES", "Multiple faces")
        if any(frame == b"blurry" for frame in frames):
            raise BiometricError("IMAGE_BLURRY", "Blurry image")
        return np.asarray([1.0, 0.0, 0.0], dtype=np.float32), 0.91

    def process_frame(self, data: bytes) -> FrameResult:
        self.create_template([data])
        observation = FaceObservation(
            raw=np.zeros(15, dtype=np.float32), confidence=0.99, box=(10, 10, 100, 100), yaw_proxy=0.0
        )
        quality = QualityResult(True, 0.91, 100.0, 128.0, "OK")
        return FrameResult(np.asarray([1.0, 0.0, 0.0], dtype=np.float32), quality, observation)

    def analyze_frame(self, data: bytes) -> FrameAnalysis:
        frame = self.process_frame(data)
        return FrameAnalysis(frame.embedding, frame.quality, frame.observation, 0.25, 320, 240)

    def analyze_liveness_frame(self, data: bytes, *, analyze_eyes: bool = True) -> FrameAnalysis:
        return self.analyze_frame(data)

    def extract_embedding(self, data: bytes) -> np.ndarray:
        return self.process_frame(data).embedding

    def verify(self, frames: list[bytes], template: np.ndarray) -> tuple[float, float, LivenessResult]:
        if any(frame == b"noface" for frame in frames):
            raise BiometricError("NO_FACE", "No face")
        score = 0.9 if frames[0] != b"mismatch" else 0.1
        return score, 0.9, LivenessResult(True, 0.88, "OK", 0.2)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        db_path=tmp_path / "biogate.db",
        model_cache=tmp_path / "models",
        attempt_image_dir=tmp_path / "snapshots",
        active_image_dir=tmp_path / "active-images",
        archive_dir=tmp_path / "archive",
        backup_dir=tmp_path / "backups",
        min_enrollment_frames=3,
        max_enrollment_frames=5,
        admin_username=TEST_ADMIN_NAME,
        admin_password_hash=TEST_ADMIN_HASH,
        session_secret=TEST_SESSION_KEY,
    )
    with TestClient(create_app(settings, FakePipeline())) as test_client:  # type: ignore[arg-type]
        login = test_client.post(
            "/api/admin/auth/login",
            json={"username": TEST_ADMIN_NAME, "password": TEST_AUTH_INPUT},
        )
        assert login.status_code == 200
        test_client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        yield test_client


@pytest.fixture
def images() -> list[tuple[str, tuple[str, bytes, str]]]:
    return [("frames", (f"frame-{index}.jpg", b"face", "image/jpeg")) for index in range(3)]


@pytest.fixture
def enrolled(client: TestClient, images: list[tuple[str, tuple[str, bytes, str]]]) -> None:
    response = client.post("/api/enroll", data={"external_id": "alice", "display_name": "Alice"}, files=images)
    assert response.status_code == 201
