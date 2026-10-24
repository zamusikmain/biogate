import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services.biometrics.embedding import serialize_embedding
from tests.test_v3 import v3_admin_login

PERSON_LOGIN = "erase-me"
PERSON_NAME = "Erase Me Person"


def _seed_person(client: TestClient) -> dict[str, Any]:
    """Create a person with every kind of biometric artefact the system can hold."""
    repository = client.app.state.repository
    storage = client.app.state.evidence_storage
    settings: Settings = client.app.state.settings
    auth = client.app.state.account_auth_service
    user = repository.create_account(
        {
            "external_id": PERSON_LOGIN,
            "login": PERSON_LOGIN,
            "full_name": PERSON_NAME,
            "password_hash": auth.hash_password("Erase-me-pass-123"),
        }
    )
    user_id = int(user["id"])
    embedding = serialize_embedding(np.asarray([1.0, 0.0, 0.0], dtype=np.float32))
    repository.upsert_template(PERSON_LOGIN, "TestEmbedding", "1", embedding, 3, 2, [(embedding, 3), (embedding, 3)])
    access_id = repository.add_access_event(
        {
            "user_id": user_id,
            "method": "FACE",
            "result": "SUCCESS",
            "reason": "IDENTIFIED",
            "client_address": "203.0.113.7",
            "user_agent": "PersonBrowser/1.0",
        }
    )
    active = storage.save(b"active-face", "image/jpeg", "FACE_IDENTIFIED", user_id=user_id, access_event_id=access_id)
    archived = storage.save(b"archived-face", "image/jpeg", "FACE_IDENTIFIED", user_id=user_id)
    with repository.db.write() as conn:
        conn.execute("UPDATE evidence_photos SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (archived["id"],))
    assert storage.archive_due(datetime.now(UTC)) == 1
    unrelated = storage.save(b"someone-else", "image/jpeg", "FACE_UNKNOWN")

    attempt_id = repository.add_verification(
        {
            "user_id": user_id,
            "claimed_external_id": PERSON_LOGIN,
            "timestamp": datetime.now(UTC).isoformat(),
            "face_detected": 1,
            "quality_score": 0.9,
            "liveness_score": 0.9,
            "similarity_score": 0.9,
            "threshold": 0.363,
            "result": "VERIFIED",
            "reason_code": "OK",
            "inference_time_ms": 1.0,
        }
    )
    settings.attempt_image_dir.mkdir(parents=True, exist_ok=True)
    snapshot = settings.attempt_image_dir / "person-snapshot.jpg"
    snapshot.write_bytes(b"snapshot-face")
    now = datetime.now(UTC)
    repository.add_snapshot(attempt_id, snapshot.name, now.isoformat(), (now + timedelta(days=7)).isoformat())

    repository.add_audit("PROFILE_NOTE", user_id, "SUCCESS", {"login": PERSON_LOGIN, "full_name": PERSON_NAME})
    repository.add_security_event("LOGIN_FAILED", "WARNING", user_id, {"login": PERSON_LOGIN})
    repository.add_security_event("AMBIGUOUS_FACE", "WARNING", None, {"best_user_id": user_id, "second_user_id": 999})
    return {
        "user_id": user_id,
        "active": active,
        "archived": repository.get_evidence_photo(str(archived["id"])),
        "unrelated": unrelated,
        "snapshot": snapshot,
    }


def _rows(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def test_erasure_removes_person_templates_samples_files_and_identifying_event_data(client: TestClient) -> None:
    v3_admin_login(client)
    seeded = _seed_person(client)
    settings: Settings = client.app.state.settings
    storage = client.app.state.evidence_storage
    user_id = seeded["user_id"]
    active_path = storage.resolve(seeded["active"])
    archived_path = storage.resolve(seeded["archived"])
    assert active_path.parent == settings.active_image_dir.resolve()
    assert archived_path.parent == settings.archive_dir.resolve()

    response = client.request(
        "DELETE", f"/api/v3/admin/accounts/{user_id}", json={"reason": "subject request", "confirmation": "ERASE"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["erased"] is True
    assert body["files_removed"] == 3
    assert body["files_failed"] == 0

    assert not active_path.exists()
    assert not archived_path.exists()
    assert not seeded["snapshot"].exists()
    assert storage.resolve(seeded["unrelated"]).exists()

    db = settings.db_path
    assert not _rows(db, "SELECT 1 FROM users WHERE id=?", (user_id,))
    assert not _rows(db, "SELECT 1 FROM biometric_templates WHERE user_id=?", (user_id,))
    assert not _rows(db, "SELECT 1 FROM biometric_template_samples")
    assert not _rows(db, "SELECT 1 FROM account_sessions WHERE user_id=?", (user_id,))
    assert not _rows(db, "SELECT 1 FROM notifications WHERE user_id=?", (user_id,))
    assert not _rows(db, "SELECT 1 FROM attempt_snapshots")
    assert [row["id"] for row in _rows(db, "SELECT id FROM evidence_photos")] == [seeded["unrelated"]["id"]]

    every_text = json.dumps(
        [
            dict(row)
            for table in ("audit_events", "security_events", "access_events", "verification_attempts")
            for row in _rows(db, f"SELECT * FROM {table}")  # noqa: S608
        ]
    )
    assert PERSON_LOGIN not in every_text
    assert PERSON_NAME not in every_text
    assert "203.0.113.7" not in every_text
    assert "PersonBrowser" not in every_text

    ambiguous = _rows(db, "SELECT metadata FROM security_events WHERE event_type='AMBIGUOUS_FACE'")
    assert json.loads(ambiguous[0]["metadata"]) == {"best_user_id": None, "second_user_id": 999}
    erased_audit = _rows(db, "SELECT user_id,metadata FROM audit_events WHERE event_type='ACCOUNT_ERASED'")
    assert len(erased_audit) == 1
    assert erased_audit[0]["user_id"] is None
    assert json.loads(erased_audit[0]["metadata"])["erasure_id"] == body["erasure_id"]

    client.cookies.delete("biogate_session")
    login = client.post("/api/v3/auth/password", json={"login": PERSON_LOGIN, "password": "Erase-me-pass-123"})
    assert login.status_code == 401


def test_erasure_requires_confirmation_reason_and_admin(client: TestClient) -> None:
    login = v3_admin_login(client)
    seeded = _seed_person(client)
    path = f"/api/v3/admin/accounts/{seeded['user_id']}"
    assert client.request("DELETE", path, json={"reason": "x", "confirmation": "erase"}).status_code == 422
    assert client.request("DELETE", path, json={"reason": "", "confirmation": "ERASE"}).status_code == 422
    missing = client.request("DELETE", "/api/v3/admin/accounts/9999", json={"reason": "x", "confirmation": "ERASE"})
    assert missing.status_code == 404
    assert missing.json()["detail"]["reason_code"] == "USER_NOT_FOUND"
    own = client.request(
        "DELETE", f"/api/v3/admin/accounts/{login.json()['user_id']}", json={"reason": "x", "confirmation": "ERASE"}
    )
    assert own.status_code == 409
    assert own.json()["detail"]["reason_code"] == "CANNOT_ERASE_OWN_ACCOUNT"

    client.cookies.delete("biogate_session")
    user_login = client.post("/api/v3/auth/password", json={"login": PERSON_LOGIN, "password": "Erase-me-pass-123"})
    client.headers["X-CSRF-Token"] = user_login.json()["csrf_token"]
    assert client.request("DELETE", path, json={"reason": "x", "confirmation": "ERASE"}).status_code == 403
    assert client.app.state.repository.get_user_by_id(seeded["user_id"]) is not None


def test_erasure_keeps_last_active_admin(client: TestClient) -> None:
    v3_admin_login(client)
    repository = client.app.state.repository
    storage = client.app.state.evidence_storage
    admin = repository.get_user_by_login("test-admin")
    assert admin is not None
    try:
        storage.erase_account(int(admin["id"]), admin_id=0, reason="test")
    except ValueError as error:
        assert str(error) == "LAST_ACTIVE_ADMIN_REQUIRED"
    else:
        raise AssertionError("last active admin must not be erasable")
    assert repository.get_user_by_id(int(admin["id"])) is not None


def test_active_image_retention_is_configurable(tmp_path: Path) -> None:
    from app.db.database import Database
    from app.services.repository import Repository
    from app.services.storage import EvidenceStorage

    settings = Settings(
        _env_file=None,
        db_path=tmp_path / "retention.db",
        active_image_dir=tmp_path / "active",
        archive_dir=tmp_path / "archive",
        active_image_days=7,
    )
    database = Database(settings.db_path)
    database.initialize()
    evidence = EvidenceStorage(Repository(database), settings)
    evidence.save(b"evidence", "image/jpeg", "TEST")
    assert evidence.archive_due(datetime.now(UTC) + timedelta(days=6)) == 0
    assert evidence.archive_due(datetime.now(UTC) + timedelta(days=8)) == 1
