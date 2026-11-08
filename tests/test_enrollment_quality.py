from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services.biometrics.errors import BiometricError
from app.services.biometrics.models import FaceObservation, QualityResult
from app.services.biometrics.pipeline import BiometricPipeline
from app.services.biometrics.quality import EnrollmentPoseGate, head_roll_degrees
from tests.conftest import create_account, enroll_photos
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


def test_pose_gate_prefers_landmark_roll_over_yunet() -> None:
    gate = EnrollmentPoseGate(max_yaw=0.35, max_roll_degrees=20.0)
    # YuNet reads a tilted head as nearly level; the MediaPipe roll decides.
    assert gate.evaluate(face(), roll_degrees=24.0) == "HEAD_TILTED"
    assert gate.evaluate(face(), roll_degrees=-24.0) == "HEAD_TILTED"
    assert gate.evaluate(face(right_eye=(80, 70)), roll_degrees=5.0) is None
    # Without landmarks the YuNet estimate is the fallback.
    assert gate.evaluate(face(right_eye=(80, 70)), roll_degrees=None) == "HEAD_TILTED"
    assert gate.evaluate(face(yaw=0.5), roll_degrees=0.0) == "HEAD_TURNED"


def test_landmark_roll_uses_outer_eye_corners_in_pixels() -> None:
    from types import SimpleNamespace

    from app.services.biometrics.landmarks import OUTER_EYE_CORNERS, EyeGeometryService

    def service(first: tuple[float, float], second: tuple[float, float] | None) -> EyeGeometryService:
        points = [SimpleNamespace(x=0.5, y=0.5) for _ in range(478)]
        points[OUTER_EYE_CORNERS[0]] = SimpleNamespace(x=first[0], y=first[1])
        if second:
            points[OUTER_EYE_CORNERS[1]] = SimpleNamespace(x=second[0], y=second[1])
        result = SimpleNamespace(face_landmarks=[points] if second else [])
        instance = object.__new__(EyeGeometryService)
        instance.landmarker = SimpleNamespace(detect=lambda _: result)
        return instance

    square = np.zeros((200, 200, 3), dtype=np.uint8)
    assert service((0.3, 0.5), (0.7, 0.5)).roll_degrees(square) == pytest.approx(0.0)
    assert service((0.3, 0.5), (0.7, 0.7)).roll_degrees(square) == pytest.approx(26.565, abs=0.01)
    assert service((0.7, 0.5), (0.3, 0.5)).roll_degrees(square) == pytest.approx(0.0)
    # Normalized coordinates are scaled per axis: 0.2 of 100 px high over 0.4 of 400 px wide is about 7 degrees.
    wide = np.zeros((100, 400, 3), dtype=np.uint8)
    assert service((0.3, 0.5), (0.7, 0.7)).roll_degrees(wide) == pytest.approx(7.125, abs=0.01)
    assert service((0.3, 0.5), None).roll_degrees(square) is None


def test_enrollment_rejects_tilt_that_yunet_misses() -> None:
    good = QualityResult(True, 0.9, 100.0, 128.0, "OK")
    tilted = _pipeline(face(), good, roll=25.0)
    with pytest.raises(BiometricError) as error:
        tilted.process_enrollment_frame(b"frame")
    assert error.value.reason_code == "HEAD_TILTED"
    # Verification frames are not pose-gated.
    assert tilted.process_frame(b"frame").embedding.size == 2
    assert _pipeline(face(), good, roll=12.0).process_enrollment_frame(b"frame").quality.accepted is True


def _pipeline(observation: FaceObservation, quality: QualityResult, roll: float | None = 0.0) -> BiometricPipeline:
    pipeline = object.__new__(BiometricPipeline)
    pipeline.eye_geometry = type("Eyes", (), {"roll_degrees": lambda self, _: roll})()
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

    user_id = create_account(client, "pose")
    photos = [("photos", (f"{index}.jpg", b"turned" if index == 0 else b"face", "image/jpeg")) for index in range(4)]
    result = enroll_photos(client, user_id, photos).json()
    assert result["results"][0]["reason_code"] == "HEAD_TURNED"
    assert result["template_created"] is True and result["accepted_frames"] == 3


def test_pose_reasons_are_localized() -> None:
    locales = Path("app/static/locales.js").read_text(encoding="utf-8")
    guidance = Path("app/static/face-guidance-locales.js").read_text(encoding="utf-8")
    for key in ("HEAD_TURNED:", "HEAD_TILTED:"):
        assert locales.count(key) == 2
    assert "headTurned: 'Смотрите прямо" in guidance and "headTilted: 'Keep your head level" in guidance
