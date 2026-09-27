from pathlib import Path

from fastapi.testclient import TestClient


def test_health(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"


def test_enrollment_and_duplicate(client: TestClient, images: list) -> None:
    first = client.post("/api/enroll", data={"external_id": "alice", "display_name": "Alice"}, files=images)
    assert first.status_code == 201
    assert "embedding" not in first.text
    second = client.post("/api/enroll", data={"external_id": "alice", "display_name": "Alice"}, files=images)
    assert second.status_code == 409


def test_unknown_verification(client: TestClient, images: list) -> None:
    response = client.post("/api/verify", data={"external_id": "missing"}, files=images)
    assert response.status_code == 404


def test_invalid_image(client: TestClient) -> None:
    files = [("frames", (f"x-{i}.jpg", b"invalid", "image/jpeg")) for i in range(3)]
    response = client.post("/api/enroll", data={"external_id": "bad", "display_name": "Bad"}, files=files)
    assert response.status_code == 422
    assert response.json()["detail"]["reason_code"] == "INVALID_IMAGE"


def test_no_and_multiple_faces(client: TestClient) -> None:
    for payload, reason in ((b"noface", "NO_FACE"), (b"multiple", "MULTIPLE_FACES")):
        files = [("frames", (f"x-{i}.jpg", payload, "image/jpeg")) for i in range(3)]
        response = client.post("/api/enroll", data={"external_id": reason.lower(), "display_name": "Test"}, files=files)
        assert response.json()["detail"]["reason_code"] == reason


def test_verification_history_and_audit(client: TestClient, images: list, enrolled: None) -> None:
    response = client.post("/api/verify", data={"external_id": "alice"}, files=images)
    assert response.json()["result"] == "VERIFIED"
    history = client.get("/api/verifications").json()
    assert len(history) == 1 and history[0]["reason_code"] == "MATCH"
    event_types = {event["event_type"] for event in client.app.state.repository.audit_events()}
    assert {"ADMIN_LOGIN_SUCCESS", "BIOMETRIC_ENROLLED", "VERIFICATION_SUCCEEDED"} <= event_types


def test_biometric_deletion(client: TestClient, enrolled: None) -> None:
    assert client.delete("/api/users/alice/biometric").json() == {"deleted": True}
    assert client.get("/api/users/alice").json()["enrolled_at"] is None


def test_privacy_no_images_written(client: TestClient, images: list, tmp_path: Path) -> None:
    client.post("/api/enroll", data={"external_id": "private", "display_name": "Private"}, files=images)
    assert list(tmp_path.rglob("*.jpg")) == []
    body = client.get("/api/users/private").text
    assert "embedding" not in body and "face" not in body.lower()
