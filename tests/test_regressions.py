import json
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.database import Database
from app.services.biometrics.detection import FaceDetector
from app.services.biometrics.embedding import serialize_embedding
from app.services.challenges import ChallengeService, EphemeralCapture
from app.services.repository import Repository
from tests.test_v2 import analysis


def make_challenge(tmp_path: Path, *, debug: bool = False) -> tuple[ChallengeService, Repository]:
    database = Database(tmp_path / "head-pose.db")
    database.initialize()
    repository = Repository(database)
    repository.create_user("pose", "Pose User")
    repository.upsert_template(
        "pose", "test", "1", serialize_embedding(np.asarray([1.0, 0.0, 0.0], dtype=np.float32)), 3, 3
    )
    return ChallengeService(repository, Settings(db_path=database.path, debug=debug)), repository


def calibrated(service: ChallengeService, baseline: float = -0.08) -> EphemeralCapture:
    capture = EphemeralCapture()
    for yaw in (baseline - 0.01, baseline, baseline + 0.01):
        service._evaluate_pose(capture, "CENTER", yaw)
    assert capture.baseline_yaw is not None
    return capture


def test_center_baseline_calibration_and_dead_zone(tmp_path: Path) -> None:
    service, _ = make_challenge(tmp_path)
    capture = calibrated(service)
    assert abs(capture.baseline_yaw - (-0.08)) < 1e-6
    assert service._evaluate_pose(capture, "CENTER", -0.03).detected_pose == "CENTER"
    assert service._evaluate_pose(capture, "TURN_LEFT", -0.13).matches is False
    assert service._evaluate_pose(capture, "TURN_RIGHT", -0.02).matches is False


def test_physical_left_and_right_are_symmetric_and_not_swapped(tmp_path: Path) -> None:
    service, _ = make_challenge(tmp_path)
    capture = calibrated(service)
    # Positive image-space delta is physical LEFT; negative is physical RIGHT.
    left = service._evaluate_pose(capture, "TURN_LEFT", 0.09)
    right = service._evaluate_pose(capture, "TURN_RIGHT", -0.25)
    assert left.detected_pose == "TURN_LEFT" and left.matches is True
    assert right.detected_pose == "TURN_RIGHT" and right.matches is True
    assert service._evaluate_pose(capture, "TURN_RIGHT", 0.09).matches is False
    assert service._evaluate_pose(capture, "TURN_LEFT", -0.25).matches is False


def test_physical_left_regression_uses_delta_not_absolute_zero(tmp_path: Path) -> None:
    service, _ = make_challenge(tmp_path)
    capture = calibrated(service, baseline=-0.09)
    evaluation = service._evaluate_pose(capture, "TURN_LEFT", 0.08)
    assert 0.08 < 0.16  # The old absolute algorithm rejected this turn.
    assert evaluation.yaw_delta is not None and evaluation.yaw_delta >= 0.16
    assert evaluation.matches is True


def test_yunet_yaw_sign_uses_original_image_coordinates() -> None:
    raw = np.zeros(15, dtype=np.float32)
    raw[4], raw[6] = 40.0, 60.0
    raw[8] = 45.0
    assert FaceDetector.yaw_from_yunet_landmarks(raw) < 0
    raw[8] = 55.0
    assert FaceDetector.yaw_from_yunet_landmarks(raw) > 0


def test_yaw_delta_semantics_are_physical_and_baseline_relative(tmp_path: Path) -> None:
    service, _ = make_challenge(tmp_path)
    capture = calibrated(service, baseline=0.04)
    physical_left = service._evaluate_pose(capture, "TURN_LEFT", 0.22)
    physical_right = service._evaluate_pose(capture, "TURN_RIGHT", -0.14)
    assert physical_left.yaw_delta is not None and physical_left.yaw_delta >= 0.16
    assert physical_right.yaw_delta is not None and physical_right.yaw_delta <= -0.16
    assert physical_left.detected_pose == "TURN_LEFT" and physical_left.matches is True
    assert physical_right.detected_pose == "TURN_RIGHT" and physical_right.matches is True
    assert service._evaluate_pose(capture, "TURN_LEFT", -0.14).matches is False
    assert service._evaluate_pose(capture, "TURN_RIGHT", 0.22).matches is False


def test_mirrored_preview_does_not_mirror_backend_frame() -> None:
    css = Path("app/static/styles.css").read_text(encoding="utf-8")
    terminal = Path("app/static/app.js").read_text(encoding="utf-8")
    admin = Path("app/static/admin.js").read_text(encoding="utf-8")
    assert "transform:scaleX(-1)" in css
    assert ".drawImage(video,0,0" in terminal
    assert ".drawImage(video, 0, 0" in admin
    assert "scale(-1" not in terminal and "scale(-1" not in admin


def test_ru_and_en_labels_share_physical_direction_mapping() -> None:
    locales = Path("app/static/locales.js").read_text(encoding="utf-8")
    assert "TURN_LEFT:'Поверните голову влево'" in locales
    assert "TURN_RIGHT:'Поверните голову вправо'" in locales
    assert "TURN_LEFT:'Turn your head left'" in locales
    assert "TURN_RIGHT:'Turn your head right'" in locales


def test_head_pose_debug_is_development_only(tmp_path: Path) -> None:
    service, repository = make_challenge(tmp_path, debug=True)
    session = service.create("pose")
    response = None
    for yaw in (-0.09, -0.08, -0.07):
        response = service.analyze(session["id"], analysis(yaw=yaw), b"frame")
    assert response is not None
    assert response["debug"]["expectedPose"] == "CENTER"
    assert response["debug"]["detectedPose"] == "CENTER"
    assert response["debug"]["baselineYaw"] is not None
    assert "embedding" not in json.dumps(response)

    production = ChallengeService(repository, Settings(db_path=repository.db.path, debug=False))
    production_session = production.create("pose")
    production_response = production.analyze(production_session["id"], analysis(yaw=-0.08), b"frame")
    assert "debug" not in production_response


def test_head_pose_debug_reports_physical_left(tmp_path: Path) -> None:
    service, repository = make_challenge(tmp_path, debug=True)
    session = service.create("pose")
    with repository.db.write() as connection:
        connection.execute(
            "UPDATE verification_sessions SET expected_sequence=? WHERE id=?",
            (json.dumps(["CENTER", "TURN_LEFT"]), session["id"]),
        )
    for _ in range(3):
        response = service.analyze(session["id"], analysis(yaw=0.0), b"frame")
    assert response["expected_action"] == "TURN_LEFT"
    response = service.analyze(session["id"], analysis(yaw=-0.22), b"frame")
    assert response["debug"]["expectedPose"] == "TURN_LEFT"
    assert response["debug"]["detectedPose"] == "TURN_RIGHT"
    assert response["debug"]["poseAccepted"] is False
    response = service.analyze(session["id"], analysis(yaw=0.22), b"frame")
    assert response["debug"]["expectedPose"] == "TURN_LEFT"
    assert response["debug"]["detectedPose"] == "TURN_LEFT"
    assert response["debug"]["yawDelta"] is not None and response["debug"]["yawDelta"] > 0


def test_delete_biometric_preserves_user_history_and_audit(client: TestClient, enrolled: None, images: list) -> None:
    client.post("/api/verify", data={"external_id": "alice"}, files=images)
    repository = client.app.state.repository
    user_id = repository.get_user("alice")["id"]
    history_before = repository.user_verifications("alice")
    audit_before = {event["id"] for event in repository.audit_events()}
    attempt_id = history_before[0]["id"]
    repository.add_snapshot(attempt_id, "historic.jpg", "2026-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00")
    response = client.delete("/api/admin/users/alice/biometric")
    assert response.status_code == 200 and response.json() == {"deleted": True}
    assert repository.get_user("alice") is not None
    assert repository.get_user("alice")["enrolled_at"] is None
    assert repository.get_template(user_id) is None
    assert repository.user_verifications("alice") == history_before
    assert repository.get_snapshot(attempt_id) is not None
    assert audit_before.issubset({event["id"] for event in repository.audit_events()})
    assert any(event["event_type"] == "BIOMETRIC_DELETED" for event in repository.audit_events())
    repeated = client.delete("/api/admin/users/alice/biometric")
    assert repeated.status_code == 409
    assert repeated.json()["detail"]["reason_code"] == "BIOMETRIC_NOT_ENROLLED"
    detail = client.get("/api/admin/users/alice").json()
    assert detail["biometric_template_id"] is None
    assert detail["model_name"] is None and detail["sample_count"] is None


def test_verification_after_delete_reports_biometric_not_enrolled(client: TestClient, enrolled: None) -> None:
    client.delete("/api/admin/users/alice/biometric")
    response = client.post("/api/verification-sessions", json={"external_id": "alice"})
    assert response.status_code == 404
    assert response.json()["detail"]["reason_code"] == "BIOMETRIC_NOT_ENROLLED"


def test_reenrollment_after_delete_keeps_old_history(client: TestClient, enrolled: None, images: list) -> None:
    client.post("/api/verify", data={"external_id": "alice"}, files=images)
    old_attempt = client.get("/api/admin/users/alice/verifications").json()[0]["id"]
    client.delete("/api/admin/users/alice/biometric")
    photos = [("photos", (f"new-{index}.jpg", b"face", "image/jpeg")) for index in range(3)]
    response = client.post("/api/admin/users/alice/enrollment/photos", files=photos)
    assert response.json()["template_created"] is True
    detail = client.get("/api/admin/users/alice").json()
    assert detail["biometric_template_id"] is not None and detail["sample_count"] == 3
    assert client.get("/api/admin/users/alice/verifications").json()[0]["id"] == old_attempt


def test_updating_existing_template_keeps_user_and_writes_audit(client: TestClient, enrolled: None) -> None:
    before = client.app.state.repository.get_user("alice")
    photos = [("photos", (f"update-{index}.jpg", b"face", "image/jpeg")) for index in range(3)]
    assert client.post("/api/admin/users/alice/enrollment/photos", files=photos).json()["template_created"] is True
    after = client.app.state.repository.get_user("alice")
    assert before["id"] == after["id"]
    assert client.app.state.repository.audit_events()[0]["event_type"] == "BIOMETRIC_UPDATED"


def test_photo_enrollment_returns_each_invalid_result(client: TestClient) -> None:
    client.post("/api/admin/users", json={"external_id": "validation", "display_name": "Validation"})
    photos = [
        ("photos", ("bad.txt", b"text", "text/plain")),
        ("photos", ("invalid.jpg", b"invalid", "image/jpeg")),
        ("photos", ("multiple.jpg", b"multiple", "image/jpeg")),
    ]
    response = client.post("/api/admin/users/validation/enrollment/photos", files=photos)
    assert response.status_code == 200
    body = response.json()
    assert body["template_created"] is False
    assert [item["reason_code"] for item in body["results"]] == [
        "UNSUPPORTED_MEDIA_TYPE",
        "INVALID_IMAGE",
        "MULTIPLE_FACES",
    ]


def test_webcam_candidate_validation_does_not_enroll(client: TestClient) -> None:
    client.post("/api/admin/users", json={"external_id": "camera", "display_name": "Camera"})
    response = client.post(
        "/api/admin/users/camera/enrollment/validate",
        files={"photo": ("candidate.jpg", b"face", "image/jpeg")},
    )
    assert response.status_code == 200 and response.json()["accepted"] is True
    assert client.get("/api/admin/users/camera").json()["biometric_template_id"] is None


def test_admin_empty_state_and_localized_delete_flow_are_present() -> None:
    source = Path("app/static/admin.js").read_text(encoding="utf-8")
    locales = Path("app/static/locales.js").read_text(encoding="utf-8")
    assert "biometric-empty" in source
    assert "/api/admin/users/${encodeURIComponent(selectedUser)}/biometric" in source
    assert "Биометрия удалена" in locales
    assert "Biometric enrollment deleted" in locales
