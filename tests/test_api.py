from pathlib import Path

from fastapi.testclient import TestClient


def test_health(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"


def test_request_id_and_health_endpoints(client: TestClient) -> None:
    generated = client.get("/health/live")
    assert generated.status_code == 200
    assert generated.headers["X-Request-ID"]
    supplied = client.get("/health/live", headers={"X-Request-ID": "qa-request-123"})
    assert supplied.headers["X-Request-ID"] == "qa-request-123"
    assert client.get("/health/ready").status_code == 200


def test_admin_enrollment_does_not_return_embeddings(client: TestClient) -> None:
    client.post("/api/admin/users", json={"external_id": "alice", "display_name": "Alice"})
    photos = [("photos", (f"photo-{index}.jpg", b"face", "image/jpeg")) for index in range(3)]
    response = client.post("/api/admin/users/alice/enrollment/photos", files=photos)
    assert response.json()["template_created"] is True
    assert "embedding" not in response.text
    detail = client.get("/api/admin/users/alice").text
    assert "embedding" not in detail


def test_biometric_deletion(client: TestClient, enrolled: None) -> None:
    assert client.delete("/api/admin/users/alice/biometric").json() == {"deleted": True}
    assert client.get("/api/admin/users/alice").json()["biometric_template_id"] is None


def test_privacy_no_images_written(client: TestClient, enrolled: None, tmp_path: Path) -> None:
    assert list(tmp_path.rglob("*.jpg")) == []
