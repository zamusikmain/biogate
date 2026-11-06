from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import create_account, enroll_photos, face_photos


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
    user_id = create_account(client, "alice", "Alice")
    response = enroll_photos(client, user_id, face_photos())
    assert response.json()["template_created"] is True
    assert "embedding" not in response.text
    assert "embedding" not in client.get(f"/api/v3/admin/accounts/{user_id}/biometric").text


def test_biometric_deletion(client: TestClient, enrolled: int) -> None:
    removed = client.request("DELETE", f"/api/v3/admin/accounts/{enrolled}/biometric", json={"reason": "test"})
    assert removed.json() == {"removed": True}
    assert client.get(f"/api/v3/admin/accounts/{enrolled}/biometric").json()["template"] is None


def test_privacy_no_images_written(client: TestClient, enrolled: int, tmp_path: Path) -> None:
    assert list(tmp_path.rglob("*.jpg")) == []
