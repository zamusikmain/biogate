"""Face login challenges are rate limited per client and capped in memory."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.services.face_login import FaceLoginService


def _service(client: TestClient) -> FaceLoginService:
    service: FaceLoginService = client.app.state.face_login_service
    return service


def test_face_challenges_from_one_client_are_rate_limited(client: TestClient) -> None:
    limit = client.app.state.settings.face_login_max_attempts
    for _ in range(limit):
        assert client.post("/api/v3/face/challenges").status_code == 200
    limited = client.post("/api/v3/face/challenges")
    assert limited.status_code == 429
    assert limited.json()["detail"]["reason_code"] == "FACE_LOGIN_RATE_LIMITED"
    assert limited.headers["Retry-After"] == "60"
    with client.app.state.repository.db.connect() as connection:
        row = connection.execute(
            "SELECT method,result,reason FROM access_events WHERE reason='FACE_LOGIN_RATE_LIMITED'"
        ).fetchone()
    assert dict(row) == {"method": "FACE", "result": "DENIED", "reason": "FACE_LOGIN_RATE_LIMITED"}


def test_rate_limit_window_expires(client: TestClient) -> None:
    service = _service(client)
    limit = client.app.state.settings.face_login_max_attempts
    for _ in range(limit):
        service.create("IDENTIFY", client_address="10.0.0.1")
    with pytest.raises(PermissionError, match="FACE_LOGIN_RATE_LIMITED"):
        service.create("IDENTIFY", client_address="10.0.0.1")
    window = timedelta(seconds=client.app.state.settings.face_login_window_seconds + 1)
    for starts in service._client_starts.values():
        starts[:] = [moment - window for moment in starts]
    service.create("IDENTIFY", client_address="10.0.0.1")


def test_rate_limit_is_per_client_address(client: TestClient) -> None:
    service = _service(client)
    limit = client.app.state.settings.face_login_max_attempts
    for _ in range(limit):
        service.create("IDENTIFY", client_address="10.0.0.1")
    with pytest.raises(PermissionError):
        service.create("IDENTIFY", client_address="10.0.0.1")
    service.create("IDENTIFY", client_address="10.0.0.2")


def test_recovery_challenges_share_the_client_limit(client: TestClient) -> None:
    service = _service(client)
    limit = client.app.state.settings.face_login_max_attempts
    for index in range(limit):
        service.create("RECOVERY", login=f"someone-{index}", client_address="10.0.0.3")
    with pytest.raises(PermissionError, match="FACE_LOGIN_RATE_LIMITED"):
        service.create("RECOVERY", login="someone-else", client_address="10.0.0.3")


def test_one_client_keeps_only_its_newest_challenges(client: TestClient) -> None:
    per_client = client.app.state.settings.face_login_max_active_per_client
    created = [client.post("/api/v3/face/challenges").json()["challenge_id"] for _ in range(per_client + 2)]
    assert _service(client).active_challenges == per_client
    oldest = client.post(
        f"/api/v3/face/challenges/{created[0]}/frames", files={"frame": ("f.jpg", b"face", "image/jpeg")}
    )
    assert oldest.status_code == 404
    assert oldest.json()["detail"]["reason_code"] == "CHALLENGE_NOT_FOUND"
    newest = client.post(
        f"/api/v3/face/challenges/{created[-1]}/frames", files={"frame": ("f.jpg", b"face", "image/jpeg")}
    )
    assert newest.status_code == 200


def test_expired_challenges_are_pruned_on_create(client: TestClient) -> None:
    service = _service(client)
    for index in range(3):
        service.create("IDENTIFY", client_address=f"10.1.0.{index}")
    for state in service._states.values():
        state.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    service.create("IDENTIFY", client_address="10.1.0.9")
    assert service.active_challenges == 1


def test_process_wide_cap_refuses_new_challenges(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = client.app.state.settings
    monkeypatch.setattr(settings, "face_login_max_active_challenges", 10)
    service = _service(client)
    for index in range(10):
        service.create("IDENTIFY", client_address=f"10.2.0.{index}")
    busy = client.post("/api/v3/face/challenges")
    assert busy.status_code == 429
    assert busy.json()["detail"]["reason_code"] == "FACE_LOGIN_BUSY"
    assert service.active_challenges == 10
