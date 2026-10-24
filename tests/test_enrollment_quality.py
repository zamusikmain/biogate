from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services.biometrics.errors import BiometricError
from app.services.biometrics.models import FaceObservation, QualityResult
from app.services.biometrics.pipeline import BiometricPipeline
from app.services.biometrics.quality import EnrollmentPoseGate, head_roll_degrees
from tests.test_v3 import v3_admin_login


def face(*, yaw: float = 0.0, left_eye: tuple[float, float] = (40, 50), right_eye: tuple[float, float] = (80, 50)):  # type: ignore[no-untyped-def]
    raw = np.zeros(15, dtype=np.float32)
    raw[4:8] = [*left_eye, *right_eye]
    return FaceObservation(raw=raw, confidence=0.99, box=(10, 10, 100, 100), yaw_proxy=yaw)


def test_head_roll_from_eye_landmarks() -> None:
    assert head_roll_degrees(face().raw) == pytest.approx(0.0)
    assert head_roll_degrees(face(right_eye=(80, 50 + 40 * np.tan(np.radians(30)))).raw) == pytest.approx(30.0, abs=0.1)
    # Swapped eye order describes the same line and must not read as a 180-degree roll.
    assert head_roll_degrees(face(left_eye=(80, 50), right_eye=(40, 50)).raw) == pytest.approx(0.0)


def test_pose_gate_reasons_and_limits() -> None:
    gate = EnrollmentPoseGate(max_yaw=0.35, max_roll_degrees=20.0)
    assert gate.evaluate(face()) is None
    assert gate.evaluate(face(yaw=0.34)) is None
    assert gate.evaluate(face(yaw=-0.36)) == "HEAD_TURNED"
    assert gate.evaluate(face(right_eye=(80, 70))) == "HEAD_TILTED"  # ~26.6 degrees
    relaxed = EnrollmentPoseGate(max_yaw=0.5, max_roll_degrees=30.0)
    assert relaxed.evaluate(face(yaw=0.4, right_eye=(80, 70))) is None


def test_pose_limits_come_from_configuration() -> None:
    settings = Settings(_env_file=None, enrollment_max_yaw=0.2, enrollment_max_roll_degrees=10.0)
    assert settings.enrollment_max_yaw == 0.2
    assert settings.enrollment_max_roll_degrees == 10.0
    with pytest.raises(ValueError):
        Settings(_env_file=None, enrollment_max_yaw=0.0)


def _pipeline(observation: FaceObservation, quality: QualityResult) -> BiometricPipeline:
    pipeline = object.__new__(BiometricPipeline)
    pipeline.decode = lambda _: np.zeros((120, 160, 3), dtype=np.uint8)  # type: ignore[method-assign]
    pipeline.detector = type("Detector", (), {"detect_one": lambda self, _: observation})()
    pipeline.quality = type("Quality", (), {"evaluate": lambda self, _, __: quality})()
    pipeline.embedder = type(
        "Embedder", (), {"extract": lambda self, _, __: np.asarray([1.0, 0.0], dtype=np.float32)}
    )()
    pipeline.pose_gate = EnrollmentPoseGate(0.35, 20.0)
    return pipeline


def test_enrollment_frames_are_rejected_for_pose_but_verification_frames_are_not() -> None:
    good = QualityResult(True, 0.9, 100.0, 128.0, "OK")
    turned = _pipeline(face(yaw=0.6), good)
    with pytest.raises(BiometricError) as error:
        turned.process_enrollment_frame(b"frame")
    assert error.value.reason_code == "HEAD_TURNED"
    assert turned.process_frame(b"frame").embedding.size == 2
    with pytest.raises(BiometricError) as tilted:
        turned.create_template([b"frame"])
    assert tilted.value.reason_code == "HEAD_TURNED"

    blurry = _pipeline(face(yaw=0.6), QualityResult(False, 0.2, 10.0, 128.0, "IMAGE_BLURRY"))
    with pytest.raises(BiometricError) as quality_first:
        blurry.process_enrollment_frame(b"frame")
    assert quality_first.value.reason_code == "IMAGE_BLURRY"

    straight = _pipeline(face(yaw=0.1), good)
    assert straight.process_enrollment_frame(b"frame").quality.accepted is True


def test_api_reports_pose_reason_for_user_capture_and_admin_photo_enrollment(client: TestClient) -> None:
    v3_admin_login(client)
    feedback = client.post("/api/v3/user/biometric-frame", files={"photo": ("f.jpg", b"turned", "image/jpeg")})
    assert feedback.json() == {"accepted": False, "reason_code": "HEAD_TURNED"}
    rejected = client.post(
        "/api/v3/user/biometric-requests",
        files=[("photos", (f"{index}.jpg", b"turned" if index == 0 else b"face", "image/jpeg")) for index in range(3)],
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["reason_code"] == "HEAD_TURNED"

    client.post("/api/admin/users", json={"external_id": "pose", "display_name": "Pose"})
    photos = [("photos", (f"{index}.jpg", b"turned" if index == 0 else b"face", "image/jpeg")) for index in range(4)]
    result = client.post("/api/admin/users/pose/enrollment/photos", files=photos).json()
    assert result["results"][0]["reason_code"] == "HEAD_TURNED"
    assert result["template_created"] is True and result["accepted_frames"] == 3


def test_pose_reasons_are_localized() -> None:
    locales = Path("app/static/locales.js").read_text(encoding="utf-8")
    guidance = Path("app/static/face-guidance-locales.js").read_text(encoding="utf-8")
    for key in ("HEAD_TURNED:", "HEAD_TILTED:"):
        assert locales.count(key) == 2
    assert "headTurned: 'Смотрите прямо" in guidance and "headTilted: 'Keep your head level" in guidance
