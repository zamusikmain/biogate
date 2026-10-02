import json
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.database import Database
from app.services.account_auth import AccountAuthError, AccountAuthService
from app.services.biometric_workflow import BiometricWorkflowError, BiometricWorkflowService
from app.services.biometrics.embedding import deserialize_embedding, serialize_embedding
from app.services.identification import IdentificationService
from app.services.repository import Repository
from app.services.storage import BackupService, EvidenceStorage
from tests.conftest import TEST_ADMIN_NAME, TEST_AUTH_INPUT, TEST_SESSION_KEY


def v3_admin_login(client: TestClient):  # type: ignore[no-untyped-def]
    response = client.post(
        "/api/v3/auth/password",
        json={"login": TEST_ADMIN_NAME, "password": TEST_AUTH_INPUT},
    )
    assert response.status_code == 200
    payload: dict[str, object] = response.json()
    client.headers["X-CSRF-Token"] = str(payload["csrf_token"])
    return response


@pytest.mark.parametrize(
    ("password", "reason"),
    [
        ("Aa1!aaaaaaa", "PASSWORD_TOO_SHORT"),
        ("UPPERCASE12!", "PASSWORD_LOWERCASE_REQUIRED"),
        ("lowercase12!", "PASSWORD_UPPERCASE_REQUIRED"),
        ("NoDigitsHere!", "PASSWORD_DIGIT_REQUIRED"),
        ("NoSpecial123", "PASSWORD_SPECIAL_REQUIRED"),
        ("Aa123456789 ", "PASSWORD_SPECIAL_REQUIRED"),
        ("ПАРОЛЬAa12!", "PASSWORD_TOO_SHORT"),
    ],
)
def test_password_policy_rejects_each_missing_requirement(tmp_path: Path, password: str, reason: str) -> None:
    auth = AccountAuthService(Repository(Database(tmp_path / "policy.db")), Settings(db_path=tmp_path / "policy.db"))
    with pytest.raises(AccountAuthError, match=reason):
        auth.validate_password(password)


def test_password_policy_accepts_valid_unicode_and_generated_values(tmp_path: Path) -> None:
    settings = Settings(db_path=tmp_path / "policy-valid.db")
    database = Database(settings.db_path)
    database.initialize()
    auth = AccountAuthService(Repository(database), settings)
    auth.validate_password("Valid-Ақпарат1!")
    for _ in range(100):
        generated = auth.generate_temporary_password()
        auth.validate_password(generated)


def test_additive_v3_migration_preserves_legacy_user(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE users (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               external_id TEXT NOT NULL UNIQUE,
               display_name TEXT NOT NULL,
               created_at TEXT NOT NULL,
               enrolled_at TEXT,
               status TEXT NOT NULL DEFAULT 'ACTIVE',
               comment TEXT NOT NULL DEFAULT '',
               updated_at TEXT
            )"""
        )
        connection.execute(
            "INSERT INTO users(external_id,display_name,created_at) VALUES('legacy-1','Legacy User','2025-01-01')"
        )

    database = Database(path)
    database.initialize()
    repository = Repository(database)
    user = repository.get_user("legacy-1")

    assert user is not None
    assert user["display_name"] == "Legacy User"
    assert user["login"] == "legacy-1"
    assert user["full_name"] == "Legacy User"
    assert user["role"] == "USER"


def test_first_admin_bootstrap_closes_after_admin_exists(tmp_path: Path) -> None:
    database = Database(tmp_path / "bootstrap.db")
    database.initialize()
    repository = Repository(database)
    first = repository.ensure_bootstrap_admin("first-admin", "hash-one")
    assert first["role"] == "ADMIN"
    assert first["full_name"] == "first-admin"
    second = repository.ensure_bootstrap_admin("second-admin", "hash-two")
    assert second["id"] == first["id"]
    assert repository.get_user_by_login("second-admin") is None


def test_admin_user_card_actions_and_human_labels_are_present() -> None:
    root = Path(__file__).parents[1]
    admin = (root / "app/static/admin.js").read_text(encoding="utf-8")
    locales = (root / "app/static/locales.js").read_text(encoding="utf-8")
    css = (root / "app/static/admin-extra.css").read_text(encoding="utf-8")
    for action in ("data-user-edit", "data-user-reset", "data-card-bio-review"):
        assert action in admin
    assert "--admin-control-bg" in css and "--admin-control-ink" in css
    for code in (
        "LOGIN_FAILED",
        "UNKNOWN_FACE",
        "BIOMETRIC_REVISION_REQUIRED",
        "PENDING_REVIEW",
        "REVISION_REQUIRED",
        "FACE_TOO_SMALL",
        "CRITICAL",
    ):
        assert locales.count(f"{code}:") >= 2, f"missing RU/EN mapping for {code}"
    assert "humanCode" in admin and "humanMetadata" in admin


def test_admin_console_is_consolidated_and_avoids_native_prompts() -> None:
    root = Path(__file__).parents[1]
    admin = (root / "app/static/admin.js").read_text(encoding="utf-8")
    v3 = (root / "app/static/admin-v3.js").read_text(encoding="utf-8")
    locales = (root / "app/static/locales.js").read_text(encoding="utf-8")
    combined = admin + v3
    assert "normalizeAdminView" in admin
    for old_route in ("security-events", "unknown-faces", "archive", "live", "attempts", "audit"):
        assert old_route in admin or old_route in v3
    assert "data-view=\"events\"" in v3
    assert "startAccessLive" in v3 and "data-access-filter" in v3
    assert "viewPhoto" in admin and "downloadPhoto" in admin
    assert "livenessLabel" in admin and "sessionDevice" in admin
    assert "eventMetadataDetails" in admin and "humanMetadata" in admin
    assert "best_user|second_user|margin" not in admin
    assert "prompt(" not in combined and "confirm(" not in combined
    for key in ("allStatuses", "allRoles", "anyBiometric", "eventLog", "runArchiveNow"):
        assert locales.count(f"{key}:") >= 2


def test_last_admin_invariant_is_transaction_safe(tmp_path: Path) -> None:
    database = Database(tmp_path / "admin-race.db")
    database.initialize()
    repository = Repository(database)
    first = repository.create_account(
        {"external_id": "a1", "login": "a1", "full_name": "A1", "role": "ADMIN"}
    )
    second = repository.create_account(
        {"external_id": "a2", "login": "a2", "full_name": "A2", "role": "ADMIN"}
    )

    def downgrade(user_id: int) -> str:
        try:
            repository.update_account(user_id, {"role": "USER", "reason": "race test"})
            return "UPDATED"
        except ValueError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(downgrade, [int(first["id"]), int(second["id"])]))
    assert sorted(results) == ["LAST_ACTIVE_ADMIN_REQUIRED", "UPDATED"]
    assert repository.active_admin_count() == 1
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"account_sessions", "access_events", "biometric_requests", "evidence_photos"} <= tables


def test_identification_uses_threshold_margin_and_account_status(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "identity.db",
        identification_threshold=0.363,
        identification_ambiguity_margin=0.035,
    )
    database = Database(settings.db_path)
    database.initialize()
    repository = Repository(database)
    for external_id, status, vector in (
        ("alice", "ACTIVE", np.array([1.0, 0.0, 0.0], dtype=np.float32)),
        ("bob", "ACTIVE", np.array([0.99, 0.1, 0.0], dtype=np.float32)),
        ("blocked", "BLOCKED", np.array([0.0, 1.0, 0.0], dtype=np.float32)),
    ):
        repository.create_account(
            {
                "external_id": external_id,
                "login": external_id,
                "full_name": external_id.title(),
                "status": status,
            }
        )
        repository.upsert_template(external_id, "test", "1", serialize_embedding(vector), 3, 3)

    service = IdentificationService(repository, settings)
    assert service.identify(np.array([-1.0, 0.0, 0.0], dtype=np.float32)).decision == "UNKNOWN"
    assert service.identify(np.array([1.0, 0.0, 0.0], dtype=np.float32)).decision == "AMBIGUOUS"
    assert service.identify(np.array([0.0, 1.0, 0.0], dtype=np.float32)).decision == "BLOCKED"


def test_identification_known_disabled_and_duplicate(tmp_path: Path) -> None:
    settings = Settings(db_path=tmp_path / "decisions.db", identification_ambiguity_margin=0.01)
    database = Database(settings.db_path)
    database.initialize()
    repository = Repository(database)
    for external_id, status, vector in (
        ("known", "ACTIVE", np.array([1.0, 0.0, 0.0], dtype=np.float32)),
        ("disabled", "DISABLED", np.array([0.0, 1.0, 0.0], dtype=np.float32)),
    ):
        repository.create_account(
            {"external_id": external_id, "login": external_id, "full_name": external_id, "status": status}
        )
        repository.upsert_template(external_id, "test", "1", serialize_embedding(vector), 3, 3)
    service = IdentificationService(repository, settings)
    assert service.identify(np.array([1.0, 0.0, 0.0], dtype=np.float32)).decision == "IDENTIFIED"
    assert service.identify(np.array([0.0, 1.0, 0.0], dtype=np.float32)).decision == "DISABLED"
    duplicate = service.possible_duplicate(np.array([1.0, 0.0, 0.0], dtype=np.float32), excluding_user_id=999)
    assert duplicate is not None and duplicate.external_id == "known"


def test_v3_password_session_csrf_and_logout(client: TestClient) -> None:
    login = v3_admin_login(client)
    payload = login.json()
    assert payload["role"] == "ADMIN"
    login_cookie = client.cookies.get("biogate_session")
    assert login_cookie
    assert "biogate_session=" in login.headers["set-cookie"]
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "SameSite=strict" in login.headers["set-cookie"]

    csrf = str(payload["csrf_token"])
    client.headers.pop("X-CSRF-Token", None)
    assert client.get("/api/v3/auth/me").status_code == 200
    assert client.post("/api/v3/auth/logout").status_code == 403
    assert client.post("/api/v3/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
    assert client.get("/api/v3/auth/me").status_code == 401


def test_unified_admin_session_routes_and_last_admin_protection(client: TestClient) -> None:
    login = v3_admin_login(client)
    admin_id = login.json()["user_id"]
    assert client.get("/admin").status_code == 200
    assert client.get("/api/v3/admin/dashboard").status_code == 200
    denied = client.patch(
        f"/api/v3/admin/accounts/{admin_id}",
        json={"status": "DISABLED", "reason": "maintenance"},
    )
    assert denied.status_code == 409
    assert denied.json()["detail"]["reason_code"] == "LAST_ACTIVE_ADMIN_REQUIRED"
    assert client.patch(
        f"/api/v3/admin/accounts/{admin_id}",
        json={"role": "USER", "reason": "role change"},
    ).status_code == 409


def test_account_filters_auth_methods_and_user_idor(client: TestClient) -> None:
    v3_admin_login(client)
    created = client.post(
        "/api/v3/admin/accounts",
        json={
            "external_id": "search-worker",
            "login": "lookup-login",
            "full_name": "Lookup Person",
            "employee_id": "EMP-42",
            "temporary_password": "Temporary-pass-123",
        },
    )
    assert created.status_code == 201
    user_id = created.json()["id"]
    rows = client.get("/api/v3/admin/accounts", params={"search": "EMP-42", "role": "USER", "status": "ACTIVE"}).json()
    assert [row["id"] for row in rows] == [user_id]
    updated = client.patch(
        f"/api/v3/admin/accounts/{user_id}",
        json={"face_enabled": False, "biometric_recovery_enabled": False},
    )
    assert updated.status_code == 200
    assert updated.json()["face_enabled"] == 0
    client.cookies.delete("biogate_session")
    user_login = client.post("/api/v3/auth/password", json={"login": "lookup-login", "password": "Temporary-pass-123"})
    assert user_login.status_code == 200
    client.headers["X-CSRF-Token"] = user_login.json()["csrf_token"]
    assert client.get(f"/api/v3/admin/accounts/{user_id}").status_code == 403
    assert client.get(f"/api/v3/admin/accounts/{user_id}/notes").status_code == 403
    assert client.delete(f"/api/v3/admin/accounts/{user_id}/sessions/not-mine").status_code == 403


def test_create_account_validation_uniqueness_atomicity_and_audit(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = {
        "external_id": " create-worker ",
        "login": " create.login ",
        "full_name": " Create Worker ",
        "employee_id": " EMP-CREATE ",
        "department": " QA ",
        "position": " Tester ",
        "comment": " Created in regression test ",
        "temporary_password": "Temporary-pass-123",
    }
    assert client.post("/api/v3/admin/accounts", json=payload).status_code == 401
    login = v3_admin_login(client)
    csrf = login.json()["csrf_token"]
    client.headers.pop("X-CSRF-Token", None)
    assert client.post("/api/v3/admin/accounts", json=payload).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    created = client.post("/api/v3/admin/accounts", json=payload)
    assert created.status_code == 201
    account = created.json()
    assert account["external_id"] == "create-worker"
    assert account["login"] == "create.login"
    assert account["employee_id"] == "EMP-CREATE"
    assert account["comment"] == "Created in regression test"
    assert account["must_change_password"] == 1
    assert account["password_enabled"] == 1 and account["face_enabled"] == 1
    stored = client.app.state.repository.get_user_by_id(account["id"])
    assert stored and stored["password_hash"] != payload["temporary_password"]
    audit = client.app.state.repository.audit_events(limit=20)
    created_audit = next(
        row for row in audit if row["event_type"] == "ACCOUNT_CREATED" and row["user_id"] == account["id"]
    )
    assert payload["temporary_password"] not in created_audit["metadata"]

    duplicate_login = {**payload, "external_id": "other-id", "employee_id": "OTHER", "login": "create.login"}
    response = client.post("/api/v3/admin/accounts", json=duplicate_login)
    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "LOGIN_ALREADY_EXISTS"
    assert client.post("/api/v3/admin/accounts", json={**payload, "login": "other", "employee_id": "OTHER"}).json()[
        "detail"
    ]["reason_code"] == "EXTERNAL_ID_ALREADY_EXISTS"
    assert client.post(
        "/api/v3/admin/accounts",
        json={**payload, "external_id": "other-external", "login": "other-login"},
    ).json()["detail"]["reason_code"] == "EMPLOYEE_ID_ALREADY_EXISTS"
    invalid_password = client.post(
        "/api/v3/admin/accounts",
        json={
            **payload,
            "external_id": "bad-password",
            "login": "bad-password",
            "employee_id": "BAD",
            "temporary_password": "short1",
        },
    )
    assert invalid_password.status_code == 422
    assert invalid_password.json()["detail"]["reason_code"] == "PASSWORD_TOO_SHORT"
    assert client.post("/api/v3/admin/accounts", json={**payload, "role": "ROOT"}).status_code == 422
    assert client.post("/api/v3/admin/accounts", json={**payload, "status": "DELETED"}).status_code == 422

    repository = client.app.state.repository
    original_audit = repository._audit

    def fail_audit(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("forced audit failure")

    monkeypatch.setattr(repository, "_audit", fail_audit)
    try:
        try:
            repository.create_account(
                {
                    "external_id": "rollback-user",
                    "login": "rollback-user",
                    "full_name": "Rollback User",
                    "password_hash": "not-plaintext",
                }
            )
        except RuntimeError:
            pass
    finally:
        monkeypatch.setattr(repository, "_audit", original_audit)
    assert repository.get_user_by_login("rollback-user") is None


def test_create_generated_password_second_admin_and_csrf(client: TestClient) -> None:
    v3_admin_login(client)
    generated = client.post(
        "/api/v3/admin/accounts",
        json={
            "external_id": "generated-user",
            "login": "generated-user",
            "full_name": "Generated User",
            "generate_temporary_password": True,
        },
    )
    assert generated.status_code == 201
    assert generated.json()["temporary_password"]
    assert generated.json()["must_change_password"] == 1
    second_admin = client.post(
        "/api/v3/admin/accounts",
        json={
            "external_id": "second-admin",
            "login": "second-admin",
            "full_name": "Second Admin",
            "role": "ADMIN",
            "temporary_password": "Temporary-admin-123",
        },
    )
    assert second_admin.status_code == 201 and second_admin.json()["role"] == "ADMIN"
    client.cookies.delete("biogate_session")
    signed_in = client.post(
        "/api/v3/auth/password",
        json={"login": "second-admin", "password": "Temporary-admin-123"},
    )
    assert signed_in.status_code == 200
    assert signed_in.json()["must_change_password"] is True
    changed = client.post(
        "/api/v3/auth/change-password",
        headers={"X-CSRF-Token": signed_in.json()["csrf_token"]},
        json={
            "current_password": "Temporary-admin-123",
            "new_password": "Permanent-admin-456",
            "confirmation": "Permanent-admin-456",
        },
    )
    assert changed.status_code == 204
    assert client.get("/api/v3/admin/accounts").status_code == 200
    assert client.get("/admin").status_code == 200


def test_private_evidence_preview_download_archive_and_integrity(client: TestClient) -> None:
    login = v3_admin_login(client)
    repository = client.app.state.repository
    storage = client.app.state.evidence_storage
    photo = storage.save(b"jpeg-evidence", "image/jpeg", "TEST")
    preview = client.get(f"/api/v3/photos/{photo['id']}")
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/jpeg"
    assert preview.headers["content-disposition"].startswith("inline;")
    assert preview.headers["content-disposition"].endswith('.jpg"')
    assert preview.headers["cache-control"] == "private, no-store, max-age=0"
    download = client.get(f"/api/v3/photos/{photo['id']}/download")
    assert download.status_code == 200
    assert download.headers["content-type"] == "image/jpeg"
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["content-disposition"].endswith('.jpg"')
    assert any(
        row["event_type"] == "EVIDENCE_DOWNLOADED" for row in repository.audit_events(limit=20)
    )

    with repository.db.write() as conn:
        conn.execute("UPDATE evidence_photos SET created_at=? WHERE id=?", ("2020-01-01T00:00:00+00:00", photo["id"]))
    assert storage.archive_due(datetime.now(UTC)) == 1
    assert client.get(f"/api/v3/photos/{photo['id']}").status_code == 200

    archived = repository.get_evidence_photo(photo["id"])
    assert archived
    path = storage.resolve(archived)
    path.write_bytes(b"corrupt")
    corrupt = client.get(f"/api/v3/photos/{photo['id']}")
    assert corrupt.status_code == 409
    assert corrupt.json()["detail"]["reason_code"] == "EVIDENCE_INTEGRITY_FAILED"

    missing = storage.save(b"png-evidence", "image/png", "TEST")
    storage.resolve(missing).unlink()
    assert client.get(f"/api/v3/photos/{missing['id']}").status_code == 404
    assert client.get("/api/v3/photos/not-a-photo").status_code == 404

    admin_id = login.json()["user_id"]
    repository.create_account(
        {
            "external_id": "evidence-user",
            "login": "evidence-user",
            "full_name": "Evidence User",
            "password_hash": client.app.state.account_auth_service.hash_password("Evidence-pass-123"),
        }
    )
    client.cookies.delete("biogate_session")
    user_login = client.post("/api/v3/auth/password", json={"login": "evidence-user", "password": "Evidence-pass-123"})
    client.headers["X-CSRF-Token"] = user_login.json()["csrf_token"]
    assert client.get(f"/api/v3/photos/{photo['id']}").status_code == 404
    assert admin_id != user_login.json()["user_id"]


def test_admin_pagination_and_safe_event_details_rbac(client: TestClient) -> None:
    v3_admin_login(client)
    repository = client.app.state.repository
    for index in range(5):
        repository.create_account(
            {
                "external_id": f"page-{index}",
                "login": f"page-{index}",
                "full_name": f"Page User {index}",
                "employee_id": f"PAGE-{index}",
            }
        )
    first = client.get("/api/v3/admin/accounts", params={"limit": 2, "offset": 0, "search": "Page User"})
    second = client.get("/api/v3/admin/accounts", params={"limit": 2, "offset": 2, "search": "Page User"})
    assert first.status_code == 200 and len(first.json()) == 2
    assert second.status_code == 200 and len(second.json()) == 2
    assert {row["id"] for row in first.json()}.isdisjoint({row["id"] for row in second.json()})
    assert client.get("/api/v3/admin/accounts", params={"limit": 0}).status_code == 422
    assert client.get("/api/v3/admin/accounts", params={"offset": -1}).status_code == 422

    event_id = repository.add_security_event(
        "SAFE_SERIALIZATION_TEST",
        "INFO",
        None,
        {"reason": "visible", "session_token": "hidden", "embedding": [1, 2, 3]},
    )
    details = client.get(f"/api/v3/admin/events/security/{event_id}")
    assert details.status_code == 200
    assert details.json()["metadata"] == {"reason": "visible"}
    assert "token" not in details.text and "embedding" not in details.text

    repository.add_audit(
        "SAFE_AUDIT_LIST_TEST",
        None,
        "SUCCESS",
        {"reason": "visible", "session_id": "hidden", "password_hash": "hidden"},
    )
    audit_list = client.get("/api/admin/audit", params={"limit": 20})
    assert audit_list.status_code == 200
    audit_event = next(row for row in audit_list.json() if row["event_type"] == "SAFE_AUDIT_LIST_TEST")
    assert audit_event["metadata"] == {"reason": "visible"}
    assert "hidden" not in str(audit_event)

    user = repository.create_account(
        {
            "external_id": "event-user",
            "login": "event-user",
            "full_name": "Event User",
            "password_hash": client.app.state.account_auth_service.hash_password("Event-user-123"),
        }
    )
    client.cookies.delete("biogate_session")
    signed_in = client.post("/api/v3/auth/password", json={"login": user["login"], "password": "Event-user-123"})
    client.headers["X-CSRF-Token"] = signed_in.json()["csrf_token"]
    assert client.get(f"/api/v3/admin/events/security/{event_id}").status_code == 403


def test_security_headers_and_sensitive_cache_policy(client: TestClient) -> None:
    for path in ("/", "/admin/login", "/health"):
        response = client.get(path)
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]

    protected = client.get("/api/v3/admin/accounts")
    assert protected.status_code == 401
    assert {part.strip() for part in protected.headers["cache-control"].split(",")} == {
        "private",
        "no-store",
        "max-age=0",
    }


def test_sqlite_operational_error_is_controlled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    v3_admin_login(client)

    def unavailable(*args: object, **kwargs: object) -> list[dict[str, object]]:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(client.app.state.repository, "list_accounts", unavailable)
    response = client.get("/api/v3/admin/accounts")
    assert response.status_code == 503
    assert response.json()["detail"]["reason_code"] == "DATABASE_TEMPORARILY_UNAVAILABLE"
    assert "locked" not in response.text


def test_v3_password_throttling_hides_unknown_account(client: TestClient) -> None:
    statuses = [
        client.post("/api/v3/auth/password", json={"login": "does-not-exist", "password": "Wrong-password-1"})
        for _ in range(6)
    ]
    assert all(response.json()["detail"]["reason_code"] == "INVALID_CREDENTIALS" for response in statuses[:5])
    assert statuses[-1].status_code == 429
    assert statuses[-1].json()["detail"]["reason_code"] == "LOGIN_RATE_LIMITED"


def test_temporary_password_requires_change_and_admin_reset_revokes_sessions(client: TestClient) -> None:
    v3_admin_login(client)
    created = client.post(
        "/api/v3/admin/accounts",
        json={
            "external_id": "worker-1",
            "login": "worker-1",
            "full_name": "Worker One",
            "temporary_password": "Temporary-pass-123",
        },
    )
    assert created.status_code == 201
    user_id = created.json()["id"]
    client.cookies.delete("biogate_session")
    signed_in = client.post(
        "/api/v3/auth/password",
        json={"login": "worker-1", "password": "Temporary-pass-123"},
    )
    assert signed_in.status_code == 200
    assert signed_in.json()["must_change_password"] is True
    user_csrf = signed_in.json()["csrf_token"]
    assert client.get("/api/v3/user/profile").status_code == 403
    changed = client.post(
        "/api/v3/auth/change-password",
        headers={"X-CSRF-Token": user_csrf},
        json={
            "current_password": "Temporary-pass-123",
            "new_password": "Permanent-pass-456",
            "confirmation": "Permanent-pass-456",
        },
    )
    assert changed.status_code == 204
    assert client.get("/api/v3/user/profile").status_code == 200
    assert client.get("/api/v3/admin/accounts").status_code == 403
    client.cookies.delete("biogate_session")
    permanent_login = client.post(
        "/api/v3/auth/password",
        json={"login": "worker-1", "password": "Permanent-pass-456"},
    )
    assert permanent_login.status_code == 200
    assert permanent_login.json()["must_change_password"] is False
    assert client.get("/user").status_code == 200
    assert client.get("/api/v3/admin/accounts").status_code == 403
    client.cookies.delete("biogate_session")
    v3_admin_login(client)
    reset = client.post(
        f"/api/v3/admin/accounts/{user_id}/reset-password",
        json={"temporary_password": "Reset-pass-789", "reason": "Support request"},
    )
    assert reset.status_code == 200
    assert reset.json()["temporary_password"] is None
    client.cookies.delete("biogate_session")
    assert (
        client.post(
            "/api/v3/auth/password",
            json={"login": "worker-1", "password": "Permanent-pass-456"},
        ).status_code
        == 401
    )
    client.cookies.delete("biogate_session")
    v3_admin_login(client)
    permanent_reset = client.post(
        f"/api/v3/admin/accounts/{user_id}/reset-password",
        json={
            "mode": "PERMANENT",
            "new_password": "Admin-set-permanent-852",
            "confirmation": "Admin-set-permanent-852",
            "reason": "Approved permanent reset",
        },
    )
    assert permanent_reset.status_code == 200
    client.cookies.delete("biogate_session")
    permanent_reset_login = client.post(
        "/api/v3/auth/password",
        json={"login": "worker-1", "password": "Admin-set-permanent-852"},
    )
    assert permanent_reset_login.status_code == 200
    assert permanent_reset_login.json()["must_change_password"] is False
    client.cookies.delete("biogate_session")
    v3_admin_login(client)
    generated_reset = client.post(
        f"/api/v3/admin/accounts/{user_id}/reset-password",
        json={"mode": "TEMPORARY", "generate": True, "reason": "Generated temporary credential"},
    )
    assert generated_reset.status_code == 200
    generated_password = generated_reset.json()["temporary_password"]
    assert isinstance(generated_password, str)
    client.cookies.delete("biogate_session")
    generated_login = client.post(
        "/api/v3/auth/password", json={"login": "worker-1", "password": generated_password}
    )
    assert generated_login.status_code == 200
    assert generated_login.json()["must_change_password"] is True
    with client.app.state.repository.db.connect() as connection:
        metadata = connection.execute(
            "SELECT metadata FROM audit_events WHERE event_type='PASSWORD_RESET_BY_ADMIN' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()["metadata"]
    assert generated_password not in metadata


def test_admin_password_reset_reason_is_validated_persisted_and_safely_exposed(client: TestClient) -> None:
    v3_admin_login(client)
    created = client.post(
        "/api/v3/admin/accounts",
        json={
            "external_id": "audit-reset-user",
            "login": "audit-reset-user",
            "full_name": "Audit Reset User",
            "temporary_password": "Initial-pass-123!",
        },
    )
    assert created.status_code == 201
    user_id = int(created.json()["id"])
    repository = client.app.state.repository

    before = repository.get_user_by_id(user_id)
    before_sessions = repository.list_account_sessions(user_id)
    for payload in (
        {"mode": "TEMPORARY", "temporary_password": "Reset-pass-123!", "reason": "   "},
        {
            "mode": "PERMANENT",
            "new_password": "Permanent-pass-123!",
            "confirmation": "Permanent-pass-123!",
            "reason": "   ",
        },
    ):
        response = client.post(f"/api/v3/admin/accounts/{user_id}/reset-password", json=payload)
        assert response.status_code == 422

    after_rejected = repository.get_user_by_id(user_id)
    assert after_rejected["password_hash"] == before["password_hash"]  # type: ignore[index]
    assert after_rejected["must_change_password"] == before["must_change_password"]  # type: ignore[index]
    assert repository.list_account_sessions(user_id) == before_sessions
    with repository.db.connect() as connection:
        rejected_audits = connection.execute(
            "SELECT COUNT(*) AS count FROM audit_events WHERE user_id=? AND event_type='PASSWORD_RESET_BY_ADMIN'",
            (user_id,),
        ).fetchone()["count"]
    assert rejected_audits == 0

    reason = "  <script>alert(1)</script> Requested by user  "
    successful = client.post(
        f"/api/v3/admin/accounts/{user_id}/reset-password",
        json={"mode": "TEMPORARY", "temporary_password": "Reset-pass-456!", "reason": reason},
    )
    assert successful.status_code == 200

    with repository.db.connect() as connection:
        reset_audits = connection.execute(
            "SELECT COUNT(*) AS count FROM audit_events WHERE user_id=? AND event_type='PASSWORD_RESET_BY_ADMIN'",
            (user_id,),
        ).fetchone()["count"]
    assert reset_audits == 1

    timeline = client.get(f"/api/v3/admin/accounts/{user_id}/timeline")
    assert timeline.status_code == 200
    reset_event = next(row for row in timeline.json() if row["event"] == "PASSWORD_RESET_BY_ADMIN")
    administrator_name = repository.get_user_by_login(TEST_ADMIN_NAME)["full_name"]  # type: ignore[index]
    assert reset_event["metadata"]["reason"] == reason.strip()
    assert reset_event["metadata"]["administrator"] == administrator_name
    assert "temporary_password" not in json.dumps(reset_event["metadata"]).lower()
    assert "reset-pass-456" not in json.dumps(reset_event["metadata"]).lower()
    assert "session" not in json.dumps(reset_event["metadata"]).lower()

    audit_rows = client.get("/api/admin/audit", params={"limit": 100}).json()
    audit_event = next(row for row in audit_rows if row["event_type"] == "PASSWORD_RESET_BY_ADMIN")
    details = client.get(f"/api/v3/admin/events/audit/{audit_event['id']}")
    assert details.status_code == 200
    assert details.json()["metadata"]["reason"] == reason.strip()
    assert details.json()["metadata"]["administrator"] == administrator_name
    serialized = json.dumps(details.json()).lower()
    assert "reset-pass-456" not in serialized
    assert "password_hash" not in serialized

    security_event = next(
        row for row in client.get("/api/v3/admin/security-events", params={"limit": 100}).json()
        if row["event_type"] == "PASSWORD_RESET"
    )
    linked_details = client.get(f"/api/v3/admin/events/security/{security_event['id']}")
    assert linked_details.status_code == 200
    assert linked_details.json()["action_metadata"] == {
        "reason": reason.strip(),
        "administrator": administrator_name,
        "mode": "TEMPORARY",
        "must_change_password": True,
    }
    assert "source_audit_event_id" not in json.dumps(linked_details.json()["action_metadata"])

    linked_timeline = next(row for row in timeline.json() if row["event"] == "PASSWORD_RESET")
    assert linked_timeline["action_metadata"]["reason"] == reason.strip()


def test_access_identity_uses_real_account_fields_for_search_details_and_csv(client: TestClient) -> None:
    v3_admin_login(client)
    repository = client.app.state.repository
    user = repository.create_account(
        {"external_id": "zamir", "login": "zamir", "full_name": "Zamir Zhan", "role": "USER"}
    )
    login_only = repository.create_account(
        {"external_id": "login-only", "login": "login-only", "full_name": "", "role": "USER"}
    )
    admin = repository.get_user_by_login(TEST_ADMIN_NAME)
    assert admin is not None
    with repository.db.write() as connection:
        connection.execute(
            "UPDATE users SET full_name=?,login=? WHERE id=?",
            ("Test Administrator", "admin_test", admin["id"]),
        )
    user_event = repository.add_access_event(
        {"user_id": user["id"], "method": "PASSWORD", "result": "SUCCESS", "reason": "AUTHENTICATED"}
    )
    admin_event = repository.add_access_event(
        {"user_id": admin["id"], "method": "PASSWORD", "result": "SUCCESS", "reason": "AUTHENTICATED"}
    )
    login_only_event = repository.add_access_event(
        {"user_id": login_only["id"], "method": "PASSWORD", "result": "BLOCKED", "reason": "ACCOUNT_BLOCKED"}
    )
    repository.add_access_event(
        {"user_id": login_only["id"], "method": "PASSWORD", "result": "DISABLED", "reason": "ACCOUNT_DISABLED"}
    )
    unknown_event = repository.add_access_event(
        {"method": "FACE", "result": "UNKNOWN", "reason": "USER_NOT_FOUND"}
    )

    for query in ("zamir", " ZAMIR ", "Zamir Zhan", " admin_test ", "Test Administrator"):
        response = client.get("/api/v3/admin/access-events", params={"user": query})
        assert response.status_code == 200
        assert response.json(), query
    rows = client.get("/api/v3/admin/access-events", params={"limit": 100}).json()
    identity = next(row for row in rows if row["id"] == user_event)
    assert {"full_name", "login", "role"} <= identity.keys()
    assert identity["full_name"] == "Zamir Zhan"
    assert identity["login"] == "zamir"
    assert identity["role"] == "USER"
    fallback_identity = next(row for row in rows if row["id"] == login_only_event)
    assert fallback_identity["full_name"] == ""
    assert fallback_identity["login"] == "login-only"
    assert fallback_identity["role"] == "USER"
    assert any(row["result"] == "DISABLED" and row["login"] == "login-only" for row in rows)
    unknown = next(row for row in rows if row["id"] == unknown_event)
    assert unknown["full_name"] is None and unknown["login"] is None and unknown["role"] is None
    assert all(row.get("full_name") not in {"BioGate User", "BioGate Administrator"} for row in rows)

    details = client.get(f"/api/v3/admin/events/access/{admin_event}")
    assert details.status_code == 200
    assert details.json()["full_name"] == "Test Administrator"
    assert details.json()["login"] == "admin_test"
    assert details.json()["role"] == "ADMIN"
    exported = client.get("/api/v3/admin/access-events.csv")
    assert exported.status_code == 200
    assert "user_display_name,login,role" in exported.text
    assert "Zamir Zhan,zamir,USER" in exported.text
    assert "Test Administrator,admin_test,ADMIN" in exported.text


def test_password_login_access_event_keeps_real_identity(client: TestClient) -> None:
    repository = client.app.state.repository
    auth = client.app.state.account_auth_service
    user = repository.create_account(
        {
            "external_id": "zamir",
            "login": "zamir",
            "full_name": "Zamir Zhan",
            "role": "USER",
            "password_hash": auth.hash_password("Zamir-password-123"),
        }
    )
    login = client.post(
        "/api/v3/auth/password",
        json={"login": "zamir", "password": "Zamir-password-123"},
    )
    assert login.status_code == 200
    v3_admin_login(client)
    rows = client.get("/api/v3/admin/access-events", params={"user": "zamir"}).json()
    event = next(row for row in rows if row["user_id"] == user["id"] and row["result"] == "SUCCESS")
    assert event["full_name"] == "Zamir Zhan"
    assert event["login"] == "zamir"
    assert event["role"] == "USER"


def test_reset_authorization_is_single_use_expires_and_revokes_sessions(tmp_path: Path) -> None:
    settings = Settings(db_path=tmp_path / "recovery.db", session_secret=TEST_SESSION_KEY)
    database = Database(settings.db_path)
    database.initialize()
    repository = Repository(database)
    auth = AccountAuthService(repository, settings)
    user = repository.create_account(
        {
            "external_id": "recover",
            "login": "recover",
            "full_name": "Recovery User",
            "password_hash": auth.hash_password("Old-password-123"),
        }
    )
    session, cookie = auth.create_session(user, "PASSWORD")
    token = auth.issue_reset_authorization(int(user["id"]))
    auth.consume_reset_authorization(token, "New-password-456")
    assert auth.get_session(cookie) is None
    assert repository.get_account_session(session.id)["revoked_reason"] == "PASSWORD_SELF_SERVICE_RESET"  # type: ignore[index]
    try:
        auth.consume_reset_authorization(token, "Another-password-789")
    except AccountAuthError as error:
        assert error.reason_code == "RESET_AUTHORIZATION_INVALID"
    else:
        raise AssertionError("A reset authorization must be single-use")

    expired = auth.issue_reset_authorization(int(user["id"]))
    with database.write() as connection:
        connection.execute(
            "UPDATE password_reset_authorizations SET expires_at=? WHERE token_hash=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), auth._token_hash(expired)),
        )
    try:
        auth.consume_reset_authorization(expired, "Another-password-789")
    except AccountAuthError as error:
        assert error.reason_code == "RESET_AUTHORIZATION_INVALID"
    else:
        raise AssertionError("An expired reset authorization must fail")


def test_status_role_and_explicit_revocation_invalidate_existing_sessions(tmp_path: Path) -> None:
    settings = Settings(db_path=tmp_path / "session-state.db", session_secret=TEST_SESSION_KEY)
    database = Database(settings.db_path)
    database.initialize()
    repository = Repository(database)
    auth = AccountAuthService(repository, settings)
    admin = repository.create_account(
        {
            "external_id": "state-admin",
            "login": "state-admin",
            "full_name": "State Admin",
            "role": "ADMIN",
            "password_hash": auth.hash_password("State-admin-123"),
        }
    )
    backup_admin = repository.create_account(
        {
            "external_id": "backup-admin",
            "login": "backup-admin",
            "full_name": "Backup Admin",
            "role": "ADMIN",
            "password_hash": auth.hash_password("Backup-admin-123"),
        }
    )
    user = repository.create_account(
        {
            "external_id": "state-user",
            "login": "state-user",
            "full_name": "State User",
            "password_hash": auth.hash_password("State-user-123"),
        }
    )
    _, blocked_cookie = auth.create_session(user, "PASSWORD")
    repository.update_account(int(user["id"]), {"status": "BLOCKED", "reason": "test"})
    assert auth.get_session(blocked_cookie) is None
    repository.update_account(int(user["id"]), {"status": "ACTIVE"})
    _, disabled_cookie = auth.create_session(user, "PASSWORD")
    repository.update_account(int(user["id"]), {"status": "DISABLED", "reason": "test"})
    assert auth.get_session(disabled_cookie) is None
    _, admin_cookie = auth.create_session(admin, "PASSWORD")
    repository.update_account(int(admin["id"]), {"role": "USER", "reason": "test"})
    assert auth.get_session(admin_cookie) is None
    _, revoked_cookie = auth.create_session(backup_admin, "PASSWORD")
    parsed = auth.get_session(revoked_cookie)
    assert parsed
    assert repository.revoke_session(parsed.id, "TEST")
    assert auth.get_session(revoked_cookie) is None


def test_biometric_revision_can_be_resubmitted(tmp_path: Path) -> None:
    settings = Settings(db_path=tmp_path / "workflow.db")
    database = Database(settings.db_path)
    database.initialize()
    repository = Repository(database)
    user = repository.create_account({"external_id": "u1", "login": "u1", "full_name": "User One"})
    workflow = BiometricWorkflowService(repository, IdentificationService(repository, settings))
    vector = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    old_vector = np.array([0.0, 1.0, 0.0], dtype=np.float32)
    repository.upsert_template("u1", "test", "1", serialize_embedding(old_vector), 3, 3)

    first = workflow.submit(int(user["id"]), vector, "test", "1", 3)
    workflow.review(str(first["id"]), "REVISION_REQUIRED", None, "Retake in better light")
    active_before = repository.get_template(int(user["id"]))
    assert active_before is not None
    assert np.allclose(deserialize_embedding(active_before["embedding"], 3), old_vector)
    second = workflow.submit(int(user["id"]), vector, "test", "1", 3)
    workflow.review(str(second["id"]), "APPROVED", None)

    assert second["id"] != first["id"]
    assert repository.get_biometric_request(str(first["id"]))["status"] == "CANCELLED"  # type: ignore[index]
    active_after = repository.get_template(int(user["id"]))
    assert active_after is not None
    assert np.allclose(deserialize_embedding(active_after["embedding"], 3), vector)


def test_concurrent_biometric_submit_has_one_pending_request(tmp_path: Path) -> None:
    settings = Settings(db_path=tmp_path / "concurrent-workflow.db")
    database = Database(settings.db_path)
    database.initialize()
    repository = Repository(database)
    user = repository.create_account({"external_id": "race", "login": "race", "full_name": "Race User"})
    workflow = BiometricWorkflowService(repository, IdentificationService(repository, settings))
    vector = np.array([1.0, 0.0, 0.0], dtype=np.float32)

    def submit() -> str:
        try:
            return str(workflow.submit(int(user["id"]), vector, "test", "1", 3)["status"])
        except BiometricWorkflowError as error:
            return error.reason_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: submit(), range(2)))

    assert sorted(outcomes) == ["ACTIVE_BIOMETRIC_REQUEST_EXISTS", "PENDING_REVIEW"]
    with database.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM biometric_requests WHERE user_id=? AND status='PENDING_REVIEW'",
            (user["id"],),
        ).fetchone()[0]
    assert count == 1


def test_evidence_archive_integrity_and_backup(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "storage.db",
        active_image_dir=tmp_path / "active",
        archive_dir=tmp_path / "archive",
        backup_dir=tmp_path / "backups",
        session_secret=TEST_SESSION_KEY,
    )
    database = Database(settings.db_path)
    database.initialize()
    repository = Repository(database)
    evidence = EvidenceStorage(repository, settings)
    photo = evidence.save(b"selected-evidence", "image/jpeg", "UNKNOWN")
    assert Path(photo["file_name"]).stem != "UNKNOWN"
    assert len(photo["sha256"]) == 64

    archived = evidence.archive_due(datetime.now(UTC) + timedelta(days=61))
    assert archived == 1
    assert evidence.archive_due(datetime.now(UTC) + timedelta(days=61)) == 0
    archived_record = repository.get_evidence_photo(str(photo["id"]))
    assert archived_record is not None
    evidence.resolve(archived_record).write_bytes(b"tampered")
    assert evidence.verify_integrity(str(photo["id"])) is False
    assert repository.get_evidence_photo(str(photo["id"]))["integrity_status"] == "INTEGRITY_FAILED"  # type: ignore[index]

    backup = BackupService(repository, settings)
    record = backup.create()
    assert backup.validate(str(record["id"])) is True
    with zipfile.ZipFile(settings.backup_dir / str(record["file_name"])) as archive:
        names = archive.namelist()
    assert "database/biogate.db" in names
    assert all(".env" not in name for name in names)


def test_backup_restore_validates_and_creates_safety_backup(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "restore.db",
        active_image_dir=tmp_path / "active",
        archive_dir=tmp_path / "archive",
        backup_dir=tmp_path / "backups",
    )
    database = Database(settings.db_path)
    database.initialize()
    repository = Repository(database)
    repository.create_account({"external_id": "before", "login": "before", "full_name": "Before"})
    backup = BackupService(repository, settings)
    saved = backup.create()
    repository.create_account({"external_id": "after", "login": "after", "full_name": "After"})

    result = backup.restore(str(saved["id"]))

    assert result["restored"] is True
    assert repository.get_user("before") is not None
    assert repository.get_user("after") is None
    safety = backup._record(str(result["safety_backup_id"]))
    assert safety is not None
    assert safety["kind"] == "SAFETY"
    assert (settings.backup_dir / str(safety["file_name"])).is_file()
    assert [row["id"] for row in backup.list()] == [saved["id"]]
