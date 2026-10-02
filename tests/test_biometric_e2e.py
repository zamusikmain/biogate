"""Biometric lifecycle integration harness with a test-only CV observation adapter.

The adapter controls decoded observations and embeddings only. All challenge,
identification, account, workflow, evidence, audit, and RBAC decisions execute
through the real application services and HTTP API against temporary storage.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from argon2 import PasswordHasher, Type
from fastapi.testclient import TestClient
from numpy.typing import NDArray

from app.core.config import Settings
from app.main import create_app
from app.services.biometrics.embedding import aggregate, deserialize_embedding, serialize_embedding
from app.services.biometrics.errors import BiometricError
from app.services.biometrics.models import FaceObservation, FrameAnalysis, FrameResult, QualityResult

_ADMIN_LOGIN = "e2e-admin"
_ADMIN_INPUT = "e2e-only-admin-input"
_SESSION_KEY = "e2e-only-session-key-longer-than-thirty-two-bytes"
_ADMIN_HASH = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1, type=Type.ID).hash(_ADMIN_INPUT)
_USER_INPUT = "E2e-user-input-123"
_USER_UPDATED_INPUT = "E2e-user-updated-456"


class ObservationAdapter:
    """Deterministic CV boundary used only by this test module."""

    model_name = "E2E-Observation-Adapter"
    model_version = "test-only"
    vectors: dict[str, NDArray[np.float32]] = {
        "a": np.asarray([1.0, 0.0, 0.0], dtype=np.float32),
        "b": np.asarray([0.0, 1.0, 0.0], dtype=np.float32),
        "unknown": np.asarray([-0.70, -0.70, 0.0], dtype=np.float32),
        "near_c": np.asarray([0.20, 0.20, 0.96], dtype=np.float32),
        "near_d": np.asarray([0.22, 0.18, 0.96], dtype=np.float32),
        "ambiguous": np.asarray([0.21, 0.19, 0.96], dtype=np.float32),
    }

    @staticmethod
    def _parts(data: bytes) -> tuple[str, str, str]:
        decoded = data.decode("ascii")
        if decoded in {"NO_FACE", "MULTIPLE_FACES", "FACE_TOO_SMALL", "IMAGE_BLURRY", "BAD_LIGHTING"}:
            raise BiometricError(decoded, decoded)
        identity, pose, eyes = (decoded.split("|") + ["CENTER", "OPEN"])[:3]
        return identity, pose, eyes

    def process_frame(self, data: bytes) -> FrameResult:
        identity, pose, _ = self._parts(data)
        analysis = self._analysis(identity, pose, "OPEN")
        return FrameResult(analysis.embedding, analysis.quality, analysis.observation)

    def analyze_frame(self, data: bytes) -> FrameAnalysis:
        identity, pose, eyes = self._parts(data)
        return self._analysis(identity, pose, eyes)

    def analyze_liveness_frame(self, data: bytes, *, analyze_eyes: bool = True) -> FrameAnalysis:
        return self.analyze_frame(data)

    def extract_embedding(self, data: bytes) -> NDArray[np.float32]:
        return self.process_frame(data).embedding

    def create_template(self, frames: list[bytes]) -> tuple[NDArray[np.float32], float]:
        results = [self.process_frame(frame) for frame in frames]
        return aggregate([item.embedding for item in results]), 0.94

    def _analysis(self, identity: str, pose: str, eyes: str) -> FrameAnalysis:
        yaw = {"CENTER": 0.0, "LEFT": 0.22, "RIGHT": -0.22, "BETWEEN": 0.09}.get(pose, 0.0)
        ear = 0.10 if eyes == "CLOSED" else 0.25
        observation = FaceObservation(
            raw=np.zeros(15, dtype=np.float32), confidence=0.99, box=(40, 30, 140, 140), yaw_proxy=yaw
        )
        quality = QualityResult(True, 0.94, 120.0, 130.0, "OK")
        return FrameAnalysis(self.vectors[identity], quality, observation, ear, 320, 240)


@pytest.fixture
def biometric_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        db_path=tmp_path / "biometric-e2e.db",
        model_cache=tmp_path / "models",
        attempt_image_dir=tmp_path / "attempts",
        active_image_dir=tmp_path / "evidence",
        archive_dir=tmp_path / "archive",
        backup_dir=tmp_path / "backups",
        admin_username=_ADMIN_LOGIN,
        admin_password_hash=_ADMIN_HASH,
        session_secret=_SESSION_KEY,
        min_enrollment_frames=3,
        max_enrollment_frames=5,
        challenge_cooldown_ms=200,
        duplicate_biometric_threshold=1.0,
        debug=True,
    )
    with TestClient(create_app(settings, ObservationAdapter())) as client:  # type: ignore[arg-type]
        yield client


def _admin_login(client: TestClient) -> dict[str, Any]:
    response = client.post("/api/v3/auth/password", json={"login": _ADMIN_LOGIN, "password": _ADMIN_INPUT})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response.json()


def _create_account(client: TestClient, external_id: str, *, status: str = "ACTIVE") -> dict[str, Any]:
    _admin_login(client)
    response = client.post(
        "/api/v3/admin/accounts",
        json={
            "external_id": external_id,
            "login": external_id,
            "full_name": f"E2E {external_id}",
            "status": status,
            "temporary_password": _USER_INPUT,
        },
    )
    assert response.status_code == 201
    assert "password_hash" not in response.json()
    return response.json()


def _enroll(client: TestClient, external_id: str, identity: str) -> None:
    _admin_login(client)
    files = [("photos", (f"fixture-{index}.jpg", identity.encode(), "image/jpeg")) for index in range(3)]
    response = client.post(f"/api/admin/users/{external_id}/enrollment/photos", files=files)
    assert response.status_code == 200
    assert response.json()["template_created"] is True
    assert "embedding" not in str(response.json()).casefold()


def _user_login_ready(client: TestClient, login: str) -> None:
    response = client.post("/api/v3/auth/password", json={"login": login, "password": _USER_INPUT})
    assert response.status_code == 200
    csrf = response.json()["csrf_token"]
    changed = client.post(
        "/api/v3/auth/change-password",
        headers={"X-CSRF-Token": csrf},
        json={
            "current_password": _USER_INPUT,
            "new_password": _USER_UPDATED_INPUT,
            "confirmation": _USER_UPDATED_INPUT,
        },
    )
    assert changed.status_code == 204
    client.headers["X-CSRF-Token"] = csrf


def _send_frame(client: TestClient, challenge_id: str, payload: str):
    return client.post(
        f"/api/v3/face/challenges/{challenge_id}/frames",
        files={"frame": ("fixture.jpg", payload.encode(), "image/jpeg")},
    )


def _complete_challenge(
    client: TestClient,
    identity: str,
    *,
    recovery_login: str | None = None,
) -> tuple[dict[str, Any], str]:
    if recovery_login is None:
        created = client.post("/api/v3/face/challenges")
    else:
        created = client.post("/api/v3/recovery/challenges", json={"login": recovery_login})
    assert created.status_code == 200
    challenge = created.json()
    challenge_id = challenge["challenge_id"]
    result: dict[str, Any] = {}
    for action in challenge["sequence"]:
        if action == "CENTER":
            payloads = [f"{identity}|CENTER|OPEN"] * 3
        elif action == "TURN_LEFT":
            payloads = [f"{identity}|LEFT|OPEN"] * 2
        elif action == "TURN_RIGHT":
            payloads = [f"{identity}|RIGHT|OPEN"] * 2
        else:
            payloads = [
                f"{identity}|CENTER|OPEN",
                f"{identity}|CENTER|OPEN",
                f"{identity}|CENTER|CLOSED",
                f"{identity}|CENTER|OPEN",
                f"{identity}|CENTER|OPEN",
            ]
        for payload in payloads:
            response = _send_frame(client, challenge_id, payload)
            assert response.status_code == 200
            result = response.json()
        if not result.get("completed"):
            time.sleep(0.21)
    assert result["completed"] is True
    return result, challenge_id


def _template(client: TestClient, user_id: int) -> NDArray[np.float32]:
    record = client.app.state.repository.get_template(user_id)
    assert record is not None
    return deserialize_embedding(record["embedding"], int(record["embedding_dimensions"]))


def _expected(identity: str) -> NDArray[np.float32]:
    return aggregate([ObservationAdapter.vectors[identity]])


def test_enrollment_identification_decisions_status_and_evidence(biometric_client: TestClient) -> None:
    client = biometric_client
    accounts = {}
    for name, identity in (("person-a", "a"), ("person-b", "b"), ("near-c", "near_c"), ("near-d", "near_d")):
        accounts[name] = _create_account(client, name)
        _enroll(client, name, identity)
        assert np.allclose(_template(client, int(accounts[name]["id"])), _expected(identity))

    identified, completed_id = _complete_challenge(client, "a")
    assert identified["decision"] == "IDENTIFIED"
    assert identified["account"]["user_id"] == accounts["person-a"]["id"]
    assert "candidate_embedding" not in str(identified)
    assert _send_frame(client, completed_id, "a|CENTER|OPEN").status_code == 404

    unknown, _ = _complete_challenge(client, "unknown")
    assert unknown["decision"] == "UNKNOWN"
    assert "account" not in unknown

    ambiguous, _ = _complete_challenge(client, "ambiguous")
    assert ambiguous["decision"] == "AMBIGUOUS"
    assert ambiguous["best_similarity"] >= ambiguous["threshold"]
    assert ambiguous["margin"] < client.app.state.settings.identification_ambiguity_margin
    assert ambiguous["best_similarity"] - ambiguous["second_similarity"] == pytest.approx(ambiguous["margin"])

    person_a_id = int(accounts["person-a"]["id"])
    sessions_before_block = client.app.state.repository.list_account_sessions(person_a_id)
    assert any(item["login_method"] == "FACE" and item["revoked_at"] is None for item in sessions_before_block)
    _admin_login(client)
    assert client.patch(
        f"/api/v3/admin/accounts/{person_a_id}", json={"status": "BLOCKED", "reason": "E2E policy"}
    ).status_code == 200
    assert all(item["revoked_at"] for item in client.app.state.repository.list_account_sessions(person_a_id))
    blocked, _ = _complete_challenge(client, "a")
    assert blocked["decision"] == "BLOCKED"

    person_b_id = int(accounts["person-b"]["id"])
    _admin_login(client)
    assert client.patch(
        f"/api/v3/admin/accounts/{person_b_id}", json={"status": "DISABLED", "reason": "E2E policy"}
    ).status_code == 200
    disabled, _ = _complete_challenge(client, "b")
    assert disabled["decision"] == "DISABLED"

    _admin_login(client)
    inbox = client.get("/api/v3/admin/unknown-faces").json()
    assert {item["result"] for item in inbox} >= {"UNKNOWN", "AMBIGUOUS"}
    unknown_row = next(item for item in inbox if item["result"] == "UNKNOWN")
    details = client.get(f"/api/v3/admin/events/unknown/{unknown_row['id']}")
    assert details.status_code == 200 and details.json()["photo_id"]
    photo_id = details.json()["photo_id"]
    preview = client.get(f"/api/v3/photos/{photo_id}")
    download = client.get(f"/api/v3/photos/{photo_id}/download")
    assert preview.status_code == download.status_code == 200
    assert preview.headers["content-type"] == "image/jpeg"
    assert "inline" in preview.headers["content-disposition"]
    assert "attachment" in download.headers["content-disposition"]

    sources = {item["source_type"] for item in client.app.state.repository.evidence_photos()}
    assert {"FACE_IDENTIFIED", "FACE_UNKNOWN", "FACE_AMBIGUOUS", "FACE_BLOCKED", "FACE_DISABLED"} <= sources
    events = {item["event_type"] for item in client.app.state.repository.security_events(limit=100)}
    assert {"UNKNOWN_FACE", "AMBIGUOUS_FACE", "BLOCKED_USER_ATTEMPT", "DISABLED_USER_ATTEMPT"} <= events


def test_biometric_update_cancel_duplicate_and_atomic_approval(
    biometric_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = biometric_client
    owner = _create_account(client, "workflow-owner")
    _enroll(client, "workflow-owner", "a")
    owner_id = int(owner["id"])
    _user_login_ready(client, "workflow-owner")

    files_b = [("photos", (f"b-{index}.jpg", b"b", "image/jpeg")) for index in range(3)]
    first = client.post("/api/v3/user/biometric-requests", files=files_b)
    assert first.status_code == 200
    first_id = first.json()["id"]
    assert np.allclose(_template(client, owner_id), _expected("a"))
    assert "candidate_embedding" not in first.json()

    _admin_login(client)
    rejected = client.post(
        f"/api/v3/admin/biometric-requests/{first_id}/review",
        json={"decision": "REJECTED", "comment": "E2E rejection"},
    )
    assert rejected.status_code == 200
    assert np.allclose(_template(client, owner_id), _expected("a"))

    client.cookies.delete("biogate_session")
    login = client.post("/api/v3/auth/password", json={"login": "workflow-owner", "password": _USER_UPDATED_INPUT})
    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    revision = client.post("/api/v3/user/biometric-requests", files=files_b)
    revision_id = revision.json()["id"]
    _admin_login(client)
    assert client.post(
        f"/api/v3/admin/biometric-requests/{revision_id}/review",
        json={"decision": "REVISION_REQUIRED", "comment": "Retake"},
    ).status_code == 200
    assert np.allclose(_template(client, owner_id), _expected("a"))

    client.cookies.delete("biogate_session")
    login = client.post("/api/v3/auth/password", json={"login": "workflow-owner", "password": _USER_UPDATED_INPUT})
    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    approved_request = client.post("/api/v3/user/biometric-requests", files=files_b)
    approved_id = approved_request.json()["id"]
    _admin_login(client)
    workflow = client.app.state.biometric_workflow
    original_audit = client.app.state.repository._audit

    def fail_audit(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(client.app.state.repository, "_audit", fail_audit)
    with pytest.raises(RuntimeError, match="injected transaction failure"):
        workflow.review(approved_id, "APPROVED", None)
    assert np.allclose(_template(client, owner_id), _expected("a"))
    assert client.app.state.repository.get_biometric_request(approved_id)["status"] == "PENDING_REVIEW"
    monkeypatch.setattr(client.app.state.repository, "_audit", original_audit)
    workflow.review(approved_id, "APPROVED", None)
    assert np.allclose(_template(client, owner_id), _expected("b"))

    client.cookies.delete("biogate_session")
    login = client.post("/api/v3/auth/password", json={"login": "workflow-owner", "password": _USER_UPDATED_INPUT})
    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    cancellable = client.post("/api/v3/user/biometric-requests", files=files_b)
    assert client.delete(f"/api/v3/user/biometric-requests/{cancellable.json()['id']}").json() == {"cancelled": True}
    assert np.allclose(_template(client, owner_id), _expected("b"))

    duplicate = _create_account(client, "duplicate-target")
    duplicate_files = [("photos", (f"duplicate-{index}.jpg", b"b", "image/jpeg")) for index in range(3)]
    duplicate_enrollment = client.post(
        "/api/admin/users/duplicate-target/enrollment/photos", files=duplicate_files
    )
    assert duplicate_enrollment.status_code == 409
    assert duplicate_enrollment.json()["detail"]["reason_code"] == "DUPLICATE_BIOMETRIC"
    assert client.app.state.repository.get_template(int(duplicate["id"])) is None
    _enroll(client, "duplicate-target", "unknown")
    _user_login_ready(client, "duplicate-target")
    duplicate_request = client.post("/api/v3/user/biometric-requests", files=files_b)
    assert duplicate_request.status_code == 200
    assert duplicate_request.json()["duplicate_user_id"] == owner_id
    _admin_login(client)
    refused = client.post(
        f"/api/v3/admin/biometric-requests/{duplicate_request.json()['id']}/review",
        json={"decision": "APPROVED", "comment": "Must not approve"},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["reason_code"] == "POSSIBLE_DUPLICATE_REQUIRES_RESOLUTION"
    assert np.allclose(_template(client, int(duplicate["id"])), _expected("unknown"))

    summary = client.get(f"/api/v3/admin/accounts/{owner_id}/biometric").json()
    assert {item["event_type"] for item in summary["history"]} >= {
        "BIOMETRIC_REJECTED",
        "BIOMETRIC_REVISION_REQUIRED",
        "BIOMETRIC_APPROVED",
    }
    assert len(client.app.state.repository.evidence_photos()) >= 12
    assert client.app.state.repository.notifications(owner_id, "USER")


def test_biometric_request_photos_are_reviewable_and_owner_scoped(biometric_client: TestClient) -> None:
    client = biometric_client
    owner = _create_account(client, "photo-owner")
    _user_login_ready(client, "photo-owner")
    files = [("photos", (f"owner-{index}.jpg", b"a", "image/jpeg")) for index in range(3)]
    submitted = client.post("/api/v3/user/biometric-requests", files=files)
    assert submitted.status_code == 200
    request_id = submitted.json()["id"]

    own_detail = client.get("/api/v3/user/biometric-request")
    assert own_detail.status_code == 200
    own_photos = own_detail.json()["photos"]
    assert len(own_photos) == 3
    assert all(
        set(photo) == {"id", "mime_type", "created_at", "integrity_status", "size_bytes"}
        for photo in own_photos
    )
    photo_id = own_photos[0]["id"]
    own_preview = client.get(f"/api/v3/photos/{photo_id}")
    assert own_preview.status_code == 200
    assert own_preview.headers["cache-control"] == "private, no-store, max-age=0"
    assert "inline" in own_preview.headers["content-disposition"]

    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    assert client.get(f"/api/v3/photos/{photo_id}").status_code == 401

    _create_account(client, "photo-other")
    _user_login_ready(client, "photo-other")
    assert client.get(f"/api/v3/photos/{photo_id}").status_code == 404

    _admin_login(client)
    admin_detail = client.get(f"/api/v3/admin/biometric-requests/{request_id}")
    assert admin_detail.status_code == 200
    assert admin_detail.json()["user_id"] == int(owner["id"])
    assert [photo["id"] for photo in admin_detail.json()["photos"]] == [photo["id"] for photo in own_photos]
    assert "candidate_embedding" not in str(admin_detail.json())


def test_targeted_recovery_is_one_to_one_single_use_and_status_gated(biometric_client: TestClient) -> None:
    client = biometric_client
    account = _create_account(client, "recoverable")
    _enroll(client, "recoverable", "a")
    _user_login_ready(client, "recoverable")
    user_id = int(account["id"])
    active_sessions = client.app.state.repository.list_account_sessions(user_id)
    assert any(item["revoked_at"] is None for item in active_sessions)

    wrong, _ = _complete_challenge(client, "b", recovery_login="recoverable")
    assert wrong["decision"] == "DENIED"
    assert "reset_authorization" not in wrong
    recovered, _ = _complete_challenge(client, "a", recovery_login="recoverable")
    assert recovered["decision"] == "RECOVERY_VERIFIED"
    authorization = recovered["reset_authorization"]
    reset = client.post(
        "/api/v3/recovery/reset",
        json={
            "token": authorization,
            "new_password": "E2e-recovered-input-789",
            "confirmation": "E2e-recovered-input-789",
        },
    )
    assert reset.status_code == 204
    assert client.post(
        "/api/v3/recovery/reset",
        json={
            "token": authorization,
            "new_password": "E2e-reuse-must-fail-012",
            "confirmation": "E2e-reuse-must-fail-012",
        },
    ).status_code == 400
    assert all(item["revoked_at"] for item in client.app.state.repository.list_account_sessions(user_id))

    blocked = _create_account(client, "recovery-blocked")
    _enroll(client, "recovery-blocked", "b")
    blocked_id = int(blocked["id"])
    _admin_login(client)
    client.patch(
        f"/api/v3/admin/accounts/{blocked_id}", json={"status": "BLOCKED", "reason": "Recovery gate"}
    )
    denied, _ = _complete_challenge(client, "b", recovery_login="recovery-blocked")
    assert denied["decision"] == "DENIED" and "reset_authorization" not in denied

    _create_account(client, "recovery-disabled", status="DISABLED")
    _enroll(client, "recovery-disabled", "unknown")
    disabled_denied, _ = _complete_challenge(client, "unknown", recovery_login="recovery-disabled")
    assert disabled_denied["decision"] == "DENIED" and "reset_authorization" not in disabled_denied

    for _ in range(client.app.state.settings.recovery_max_attempts):
        response = client.post("/api/v3/recovery/challenges", json={"login": "rate-limited-target"})
        assert response.status_code == 200
    assert client.post(
        "/api/v3/recovery/challenges", json={"login": "rate-limited-target"}
    ).status_code == 429
    events = {item["event_type"] for item in client.app.state.repository.security_events(limit=100)}
    assert {"PASSWORD_RECOVERY_FAILED", "PASSWORD_RESET"} <= events
    assert any(item["source_type"] == "FACE_DENIED" for item in client.app.state.repository.evidence_photos())


def test_recovery_uses_representative_when_centroid_is_below_threshold_and_keeps_status_gates(
    biometric_client: TestClient,
) -> None:
    client = biometric_client
    account = _create_account(client, "representative-recovery")
    user_id = int(account["id"])
    centroid = ObservationAdapter.vectors["a"]
    representative = ObservationAdapter.vectors["b"]
    client.app.state.repository.upsert_template(
        "representative-recovery",
        "test",
        "1",
        serialize_embedding(centroid),
        3,
        1,
        [(serialize_embedding(representative), 3)],
    )
    assert float(np.dot(centroid, representative)) < client.app.state.settings.verification_threshold

    impostor, _ = _complete_challenge(client, "unknown", recovery_login="representative-recovery")
    assert impostor["decision"] == "DENIED"
    assert "reset_authorization" not in impostor

    recovered, _ = _complete_challenge(client, "b", recovery_login="representative-recovery")
    assert recovered["decision"] == "RECOVERY_VERIFIED"
    assert recovered["similarity"] > client.app.state.settings.verification_threshold
    assert recovered["reset_authorization"]

    _admin_login(client)
    assert client.patch(
        f"/api/v3/admin/accounts/{user_id}", json={"status": "BLOCKED", "reason": "Recovery gate"}
    ).status_code == 200
    blocked, _ = _complete_challenge(client, "b", recovery_login="representative-recovery")
    assert blocked["decision"] == "DENIED"
    assert "reset_authorization" not in blocked

    _admin_login(client)
    assert client.patch(
        f"/api/v3/admin/accounts/{user_id}", json={"status": "DISABLED", "reason": "Recovery gate"}
    ).status_code == 200
    disabled, _ = _complete_challenge(client, "b", recovery_login="representative-recovery")
    assert disabled["decision"] == "DENIED"
    assert "reset_authorization" not in disabled


def test_cv_failures_wrong_actions_expiry_and_semantic_diagnostics(biometric_client: TestClient) -> None:
    client = biometric_client
    _create_account(client, "pose-user")
    _enroll(client, "pose-user", "a")
    created = client.post("/api/v3/face/challenges", json={"livenessPassed": True, "sequence": ["BLINK"]})
    assert created.status_code == 200
    challenge = created.json()
    assert challenge["sequence"][0] == "CENTER"
    assert set(challenge["sequence"]) == {"CENTER", "TURN_LEFT", "TURN_RIGHT", "BLINK"}

    for reason in ("NO_FACE", "MULTIPLE_FACES", "FACE_TOO_SMALL", "IMAGE_BLURRY", "BAD_LIGHTING"):
        response = _send_frame(client, challenge["challenge_id"], reason)
        assert response.status_code == 422
        assert response.json()["detail"]["reason_code"] == reason
    assert challenge["current_step"] == 0

    for _ in range(3):
        centered = _send_frame(client, challenge["challenge_id"], "a|CENTER|OPEN")
    assert centered.json()["accepted"] is True
    time.sleep(0.21)
    expected = centered.json()["expected_action"]
    if expected == "TURN_LEFT":
        wrong_payload = "a|RIGHT|OPEN"
        expected_detected = "TURN_RIGHT"
    elif expected == "TURN_RIGHT":
        wrong_payload = "a|LEFT|OPEN"
        expected_detected = "TURN_LEFT"
    else:
        wrong_payload = "a|BETWEEN|OPEN"
        expected_detected = "BLINK"
    wrong = _send_frame(client, challenge["challenge_id"], wrong_payload)
    assert wrong.status_code == 200 and wrong.json()["accepted"] is False
    if expected != "BLINK":
        assert wrong.json()["debug"]["detectedPose"] == expected_detected
        assert wrong.json()["debug"]["expectedPose"] == expected

    expiring = client.post("/api/v3/face/challenges").json()
    state = client.app.state.face_login_service._states[expiring["challenge_id"]]
    state.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    expired = _send_frame(client, expiring["challenge_id"], "a|CENTER|OPEN")
    assert expired.status_code == 410

    labels = (Path("app/static/v3-locales.js").read_text(encoding="utf-8"))
    assert "TURN_LEFT:'Поверните голову налево'" in labels
    assert "TURN_RIGHT:'Поверните голову направо'" in labels
    assert "TURN_LEFT:'Turn your head left'" in labels
    assert "TURN_RIGHT:'Turn your head right'" in labels
    login_source = Path("app/static/login-v3.js").read_text(encoding="utf-8")
    assert "scaleX(-1)" in Path("app/static/v3.css").read_text(encoding="utf-8")
    assert "G.capture(video)" in login_source
    capture_source = Path("app/static/face-guidance.js").read_text(encoding="utf-8")
    assert "drawImage(video, 0, 0" in capture_source
    assert "scale(-1" not in capture_source


def test_reset_authorization_expiry_remains_enforced(biometric_client: TestClient) -> None:
    client = biometric_client
    account = _create_account(client, "expiry-user")
    authorization = client.app.state.account_auth_service.issue_reset_authorization(int(account["id"]))
    token_hash = client.app.state.account_auth_service._token_hash(authorization)
    with client.app.state.repository.db.write() as connection:
        connection.execute(
            "UPDATE password_reset_authorizations SET expires_at=? WHERE token_hash=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), token_hash),
        )
    response = client.post(
        "/api/v3/recovery/reset",
        json={
            "token": authorization,
            "new_password": "E2e-expired-must-fail-345",
            "confirmation": "E2e-expired-must-fail-345",
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"]["reason_code"] == "RESET_AUTHORIZATION_INVALID"
