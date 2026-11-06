import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.database import Database
from app.services.biometrics.embedding import serialize_embedding
from app.services.biometrics.models import FaceObservation, FrameAnalysis, QualityResult
from app.services.challenges import BlinkStateMachine, BlinkTemporalState, ChallengeService, EphemeralCapture
from app.services.maintenance import cleanup_expired_attempt_snapshots
from app.services.repository import Repository
from tests.conftest import create_account, enroll_photos, record_verification


def analysis(yaw: float = 0.0, ear: float = 0.25) -> FrameAnalysis:
    observation = FaceObservation(
        raw=np.zeros(15, dtype=np.float32), confidence=0.99, box=(20, 20, 120, 120), yaw_proxy=yaw
    )
    return FrameAnalysis(
        embedding=np.asarray([1.0, 0.0, 0.0], dtype=np.float32),
        quality=QualityResult(True, 0.9, 100.0, 128.0, "OK"),
        observation=observation,
        eye_aspect_ratio=ear,
        image_width=320,
        image_height=240,
    )


def test_blink_requires_open_closed_open() -> None:
    machine = BlinkStateMachine()
    state = BlinkTemporalState()
    phase, completed = machine.advance(state, 0.25)
    assert (phase, completed) == ("WAITING_OPEN", False)
    phase, completed = machine.advance(state, 0.25)
    assert (phase, completed) == ("WAITING_CLOSED", False)
    phase, completed = machine.advance(state, 0.12)
    assert (phase, completed) == ("WAITING_REOPEN", False)
    machine.advance(state, 0.25)
    phase, completed = machine.advance(state, 0.25)
    assert (phase, completed) == ("COMPLETE", True)


def test_blink_without_reopen_does_not_complete() -> None:
    machine = BlinkStateMachine()
    state = BlinkTemporalState()
    outcomes = [machine.advance(state, ear) for ear in (0.25, 0.25, 0.12)]
    assert outcomes[-1] == ("WAITING_REOPEN", False)


def test_single_bad_frame_is_not_a_blink() -> None:
    machine = BlinkStateMachine()
    state = BlinkTemporalState()
    for ear in (0.25, 0.25, None, 0.25, 0.25):
        phase, completed = machine.advance(state, ear)
    assert phase == "WAITING_CLOSED"
    assert completed is False


def test_fast_blink_in_short_consecutive_frames_is_detected() -> None:
    machine = BlinkStateMachine()
    state = BlinkTemporalState()
    outcomes = [machine.advance(state, ear) for ear in (0.24, 0.25, 0.13, 0.24, 0.25)]
    assert outcomes[-1] == ("COMPLETE", True)


def test_blink_requires_both_eyes_when_per_eye_geometry_is_available() -> None:
    machine = BlinkStateMachine()
    state = BlinkTemporalState()
    machine.advance(state, 0.25, 0.25, 0.25)
    machine.advance(state, 0.25, 0.25, 0.25)
    phase, completed = machine.advance(state, 0.17, 0.12, 0.22)
    assert phase == "WAITING_CLOSED"
    assert completed is False


def test_head_turn_invalidates_blink_progress() -> None:
    machine = BlinkStateMachine()
    state = BlinkTemporalState()
    machine.advance(state, 0.25)
    machine.advance(state, 0.25)
    machine.advance(state, 0.12, centered=False)
    machine.advance(state, 0.12, centered=False)
    machine.advance(state, 0.25)
    phase, completed = machine.advance(state, 0.25)
    assert phase == "WAITING_CLOSED"
    assert completed is False


def test_pose_direction_noise_natural_turn_and_fixed_baseline(tmp_path: Path) -> None:
    database = Database(tmp_path / "pose-temporal.db")
    database.initialize()
    repository = Repository(database)
    service = ChallengeService(repository, Settings(db_path=database.path))
    capture = EphemeralCapture()
    for yaw in (0.01, 0.0, -0.01):
        service._evaluate_pose(capture, "CENTER", yaw)
    baseline = capture.baseline_yaw
    assert baseline is not None
    assert service._evaluate_pose(capture, "TURN_LEFT", baseline + 0.04).matches is False
    assert service._evaluate_pose(capture, "TURN_LEFT", baseline + 0.18).matches is True
    assert service._evaluate_pose(capture, "TURN_RIGHT", baseline + 0.18).matches is False
    assert service._evaluate_pose(capture, "TURN_RIGHT", baseline - 0.18).matches is True
    assert capture.baseline_yaw == baseline


def test_auto_capture_requires_stability(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    database.initialize()
    repository = Repository(database)
    repository.create_user("alice", "Alice")
    repository.upsert_template(
        "alice", "test", "1", serialize_embedding(np.asarray([1.0, 0.0], dtype=np.float32)), 2, 3
    )
    service = ChallengeService(repository, Settings(db_path=tmp_path / "test.db", challenge_cooldown_ms=200))
    session = service.create("alice")
    first = service.analyze(session["id"], analysis(), b"frame")
    second = service.analyze(session["id"], analysis(), b"frame")
    third = service.analyze(session["id"], analysis(), b"frame")
    assert first["accepted"] is False
    assert second["accepted"] is False
    assert third["accepted"] is True
    assert third["current_step"] == 1


def test_embedding_is_extracted_only_for_an_accepted_challenge_frame(tmp_path: Path) -> None:
    database = Database(tmp_path / "lazy-embedding.db")
    database.initialize()
    repository = Repository(database)
    repository.create_user("alice", "Alice")
    repository.upsert_template(
        "alice", "test", "1", serialize_embedding(np.asarray([1.0, 0.0], dtype=np.float32)), 2, 1
    )
    service = ChallengeService(repository, Settings(db_path=database.path, challenge_cooldown_ms=200))
    session = service.create("alice")
    extractions = 0

    def extract() -> np.ndarray:
        nonlocal extractions
        extractions += 1
        return np.asarray([1.0, 0.0], dtype=np.float32)

    service.analyze(session["id"], analysis(), b"frame", embedding_factory=extract)
    service.analyze(session["id"], analysis(), b"frame", embedding_factory=extract)
    assert extractions == 0
    result = service.analyze(session["id"], analysis(), b"frame", embedding_factory=extract)
    assert result["accepted"] is True
    assert extractions == 1


def test_automatic_verification_session_completes(tmp_path: Path) -> None:
    database = Database(tmp_path / "complete.db")
    database.initialize()
    repository = Repository(database)
    repository.create_user("alice", "Alice")
    repository.upsert_template(
        "alice", "test", "1", serialize_embedding(np.asarray([1.0, 0.0, 0.0], dtype=np.float32)), 3, 3
    )
    service = ChallengeService(repository, Settings(db_path=database.path, challenge_cooldown_ms=200))
    session = service.create("alice")
    sequence = json.loads(session["expected_sequence"])
    result = None
    for action in sequence:
        if action == "BLINK":
            for ear in (0.25, 0.25, 0.12, 0.25, 0.25):
                result = service.analyze(session["id"], analysis(ear=ear), b"frame")
        else:
            yaw = {"CENTER": 0.0, "TURN_LEFT": 0.22, "TURN_RIGHT": -0.22}[action]
            frame_count = 3 if action == "CENTER" else 2
            for _ in range(frame_count):
                result = service.analyze(session["id"], analysis(yaw=yaw), b"frame")
        with database.write() as connection:
            connection.execute(
                "UPDATE verification_sessions SET last_capture_at=? WHERE id=?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), session["id"]),
            )
    assert result is not None and result["completed"] is True
    assert result["result"] == "VERIFIED"
    assert repository.get_session(session["id"])["state"] == "COMPLETED"
    assert repository.verifications()[0]["reason_code"] == "MATCH"


def test_invalid_challenge_sequence_is_rejected(tmp_path: Path) -> None:
    database = Database(tmp_path / "invalid-sequence.db")
    database.initialize()
    repository = Repository(database)
    repository.create_user("alice", "Alice")
    repository.upsert_template(
        "alice", "test", "1", serialize_embedding(np.asarray([1.0, 0.0, 0.0], dtype=np.float32)), 3, 3
    )
    service = ChallengeService(repository, Settings(db_path=database.path))
    session = service.create("alice")
    with database.write() as connection:
        connection.execute("UPDATE verification_sessions SET current_step=99 WHERE id=?", (session["id"],))
    try:
        service.analyze(session["id"], analysis(), b"frame")
        raise AssertionError("Invalid sequence must fail")
    except ValueError as error:
        assert str(error) == "INVALID_CHALLENGE_SEQUENCE"
    assert repository.verifications()[0]["reason_code"] == "INVALID_CHALLENGE_SEQUENCE"


def test_blocked_account_cannot_start_a_verification_challenge(client: TestClient) -> None:
    user_id = create_account(client, "blocked", status="BLOCKED")
    client.app.state.repository.upsert_template(
        "blocked", "test", "1", serialize_embedding(np.asarray([1.0, 0.0, 0.0], dtype=np.float32)), 3, 3
    )
    service = ChallengeService(client.app.state.repository, client.app.state.settings)
    with pytest.raises(PermissionError):
        service.create("blocked")
    assert client.get(f"/api/v3/admin/accounts/{user_id}").json()["status"] == "BLOCKED"


def test_admin_photo_enrollment_with_per_file_results(client: TestClient) -> None:
    user_id = create_account(client, "photo")
    files = [
        ("photos", ("good-1.jpg", b"face", "image/jpeg")),
        ("photos", ("bad.jpg", b"multiple", "image/jpeg")),
        ("photos", ("good-2.jpg", b"face", "image/jpeg")),
        ("photos", ("good-3.jpg", b"face", "image/jpeg")),
    ]
    body = enroll_photos(client, user_id, files).json()
    assert body["template_created"] is True
    assert body["accepted_frames"] == 3
    assert body["results"][1]["reason_code"] == "MULTIPLE_FACES"


def test_insufficient_valid_photo_enrollment(client: TestClient) -> None:
    user_id = create_account(client, "few")
    files = [
        ("photos", ("good.jpg", b"face", "image/jpeg")),
        ("photos", ("bad.jpg", b"invalid", "image/jpeg")),
        ("photos", ("many.jpg", b"multiple", "image/jpeg")),
    ]
    body = enroll_photos(client, user_id, files).json()
    assert body["template_created"] is False
    assert body["status"] == "INSUFFICIENT_VALID_IMAGES"


def test_historical_attempts_keep_their_audit_link(client: TestClient, enrolled: int) -> None:
    attempt_id = record_verification(client, "alice")
    repository = client.app.state.repository
    assert [row["id"] for row in repository.user_verifications("alice")] == [attempt_id]
    assert repository.get_verification(attempt_id)["claimed_external_id"] == "alice"
    linked = [event for event in repository.audit_events(500) if event.get("attempt_id") == attempt_id]
    assert [event["event_type"] for event in linked] == ["VERIFICATION_SUCCEEDED"]


def test_expired_attempt_snapshots_are_removed_at_startup(client: TestClient) -> None:
    repository = client.app.state.repository
    settings = client.app.state.settings
    attempt_id = record_verification(client, "nobody", "REJECTED")
    keep_id = record_verification(client, "nobody", "REJECTED")
    snapshot_dir = settings.attempt_image_dir
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "expired.jpg").write_bytes(b"demo")
    (snapshot_dir / "current.jpg").write_bytes(b"demo")
    now = datetime.now(UTC)
    repository.add_snapshot(attempt_id, "expired.jpg", now.isoformat(), (now - timedelta(days=1)).isoformat())
    repository.add_snapshot(keep_id, "current.jpg", now.isoformat(), (now + timedelta(days=1)).isoformat())
    assert cleanup_expired_attempt_snapshots(repository, settings) == 1
    assert not (snapshot_dir / "expired.jpg").exists()
    assert (snapshot_dir / "current.jpg").exists()
    assert repository.get_snapshot(attempt_id) is None
    assert repository.get_snapshot(keep_id) is not None


def test_localization_contains_ru_and_en() -> None:
    source = Path("app/static/locales.js").read_text(encoding="utf-8")
    assert "const ru={" in source and "const en={" in source
    assert "Личность подтверждена" in source and "Identity verified" in source


def test_migration_preserves_existing_template(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """CREATE TABLE users(
          id INTEGER PRIMARY KEY,external_id TEXT UNIQUE,display_name TEXT,
          created_at TEXT,enrolled_at TEXT);
        CREATE TABLE biometric_templates(
          id INTEGER PRIMARY KEY,user_id INTEGER UNIQUE,model_name TEXT,
          model_version TEXT,embedding BLOB,embedding_dimensions INTEGER,created_at TEXT);
        CREATE TABLE verification_attempts(
          id INTEGER PRIMARY KEY,user_id INTEGER,claimed_external_id TEXT,timestamp TEXT,
          face_detected INTEGER,quality_score REAL,liveness_score REAL,similarity_score REAL,
          threshold REAL,result TEXT,reason_code TEXT,inference_time_ms REAL);
        CREATE TABLE audit_events(
          id INTEGER PRIMARY KEY,timestamp TEXT,event_type TEXT,user_id INTEGER,
          result TEXT,metadata TEXT);
        INSERT INTO users VALUES(1,'zamir','Zamir','now','now');
        INSERT INTO biometric_templates VALUES(1,1,'OpenCV-SFace','2021dec',X'0000803F',1,'now');"""
    )
    connection.commit()
    connection.close()
    Database(path).initialize()
    repository = Repository(Database(path))
    assert repository.get_user("zamir")["status"] == "ACTIVE"
    assert repository.get_template(1)["model_version"] == "2021dec"
