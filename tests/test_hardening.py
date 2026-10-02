import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.database import Database
from app.services.repository import Repository
from app.services.risk import RISK_RULES, RiskEngine
from app.services.storage import BACKUP_FORMAT_VERSION, BACKUP_SCHEMA_VERSION, BackupService
from scripts.evaluate_biometrics import PRODUCTION_THRESHOLD, evaluate_embeddings, load_dataset, write_csv
from tests.conftest import TEST_ADMIN_NAME, TEST_AUTH_INPUT


def event(event_type: str, timestamp: datetime, user_id: int | None = 1) -> dict[str, object]:
    return {"event_type": event_type, "timestamp": timestamp.isoformat(), "user_id": user_id, "metadata": {}}


@pytest.mark.parametrize(
    ("event_type", "count", "reason", "level"),
    [
        ("LOGIN_FAILED", 5, "MULTIPLE_FAILED_LOGINS", "HIGH"),
        ("UNKNOWN_FACE", 3, "REPEATED_UNKNOWN_FACE", "HIGH"),
        ("LIVENESS_FAILED", 3, "REPEATED_BIOMETRIC_FAILURES", "HIGH"),
        ("PASSWORD_RECOVERY_FAILED", 3, "RECOVERY_ABUSE", "CRITICAL"),
        ("BLOCKED_USER_ATTEMPT", 1, "BLOCKED_ACCOUNT_ATTEMPTS", "HIGH"),
        ("DISABLED_USER_ATTEMPT", 1, "DISABLED_ACCOUNT_ATTEMPTS", "HIGH"),
        ("PASSWORD_RESET", 3, "EXCESSIVE_PASSWORD_RESET", "CRITICAL"),
    ],
)
def test_risk_rules_are_deterministic(event_type: str, count: int, reason: str, level: str) -> None:
    now = datetime.now(UTC)
    events = [event(event_type, now - timedelta(seconds=index)) for index in range(count)]
    result = RiskEngine().evaluate(events[0], events)
    assert result["reason_code"] == reason
    assert result["risk_level"] == level


def test_risk_normal_old_and_multiple_rules() -> None:
    now = datetime.now(UTC)
    normal = event("LOGIN_FAILED", now)
    assert RiskEngine().evaluate(normal, [normal])["risk_level"] == "LOW"
    medium = [event("LOGIN_FAILED", now - timedelta(seconds=index)) for index in range(3)]
    assert RiskEngine().evaluate(medium[0], medium)["risk_level"] == "MEDIUM"
    old = [event("UNKNOWN_FACE", now - timedelta(hours=1)) for _ in range(3)]
    assert RiskEngine().evaluate(event("UNKNOWN_FACE", now), [event("UNKNOWN_FACE", now), *old])["risk_level"] == "LOW"
    combined = [event("LOGIN_FAILED", now) for _ in range(5)] + [
        event("PASSWORD_RECOVERY_FAILED", now) for _ in range(3)
    ]
    result = RiskEngine().evaluate(combined[0], combined)
    assert result["risk_level"] == "CRITICAL"
    assert result["reason_code"] == "RECOVERY_ABUSE"
    assert all("biometric" not in field and "demographic" not in field for rule in RISK_RULES for field in vars(rule))


def backup_fixture(tmp_path: Path) -> tuple[BackupService, dict[str, object], Path]:
    settings = Settings(
        _env_file=None,
        db_path=tmp_path / "database.db",
        active_image_dir=tmp_path / "active",
        archive_dir=tmp_path / "archive",
        backup_dir=tmp_path / "backups",
    )
    database = Database(settings.db_path)
    database.initialize()
    repository = Repository(database)
    repository.create_account({"external_id": "backup-user", "login": "backup-user", "full_name": "Backup User"})
    service = BackupService(repository, settings)
    record = service.create()
    return service, record, settings.backup_dir / str(record["file_name"])


def rewrite_zip(path: Path, transform: object) -> None:
    with zipfile.ZipFile(path) as source:
        files = {info.filename: source.read(info.filename) for info in source.infolist() if not info.is_dir()}
    transform(files)  # type: ignore[operator]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for name, data in files.items():
            target.writestr(name, data)


def test_backup_manifest_and_restore(tmp_path: Path) -> None:
    service, record, path = backup_fixture(tmp_path)
    assert service.validate(str(record["id"]))
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["backup_format_version"] == BACKUP_FORMAT_VERSION
    assert manifest["schema_version"] == BACKUP_SCHEMA_VERSION
    assert manifest["kind"] == "USER"
    assert {item["path"] for item in manifest["files"]} >= {"database/biogate.db"}
    assert all(len(item["sha256"]) == 64 for item in manifest["files"])
    restored = service.restore(str(record["id"]))
    assert restored["restored"] is True
    assert len(service.list()) == 1
    safety = service.create(kind="SAFETY", audit=False)
    with zipfile.ZipFile(path.parent / str(safety["file_name"])) as archive:
        assert json.loads(archive.read("manifest.json"))["kind"] == "SAFETY"
    assert [row["id"] for row in service.list()] == [record["id"]]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda files: files.__setitem__("manifest.json", b"{"), "BACKUP_MANIFEST_INVALID"),
        (
            lambda files: files.__setitem__(
                "manifest.json",
                json.dumps({**json.loads(files["manifest.json"]), "backup_format_version": 999}).encode(),
            ),
            "BACKUP_VERSION_UNSUPPORTED",
        ),
        (
            lambda files: files.__setitem__(
                "manifest.json", json.dumps({**json.loads(files["manifest.json"]), "schema_version": 999}).encode()
            ),
            "BACKUP_SCHEMA_INCOMPATIBLE",
        ),
        (lambda files: files.pop("database/biogate.db"), "BACKUP_MANIFEST_FILE_MISMATCH"),
        (lambda files: files.__setitem__("database/biogate.db", b"corrupt"), "BACKUP_INTEGRITY_FAILED"),
    ],
)
def test_backup_rejects_manifest_and_file_damage(tmp_path: Path, mutation: object, reason: str) -> None:
    service, _, path = backup_fixture(tmp_path)
    rewrite_zip(path, mutation)
    with pytest.raises(ValueError, match=reason):
        service._validate_archive(path)


def test_backup_rejects_legacy_format(tmp_path: Path) -> None:
    service, _, path = backup_fixture(tmp_path)
    rewrite_zip(path, lambda files: files.pop("manifest.json"))
    with pytest.raises(ValueError, match="BACKUP_LEGACY_FORMAT"):
        service._validate_archive(path)


def test_failed_restore_does_not_create_safety_or_change_database(tmp_path: Path) -> None:
    service, record, path = backup_fixture(tmp_path)
    before = service.repository.get_user_by_login("backup-user")
    rewrite_zip(path, lambda files: files.__setitem__("database/biogate.db", b"corrupt"))
    with pytest.raises(ValueError, match="BACKUP_INTEGRITY_FAILED"):
        service.restore(str(record["id"]))
    assert service.repository.get_user_by_login("backup-user") == before
    assert len(service.list()) == 1


def test_benchmark_metrics_and_outputs(tmp_path: Path) -> None:
    identities = {
        "one": [np.array([1.0, 0.0], dtype=np.float32), np.array([0.99, 0.01], dtype=np.float32)],
        "two": [np.array([0.0, 1.0], dtype=np.float32)],
    }
    report = evaluate_embeddings(identities, [0.5])
    assert report["identities"] == 2
    assert report["images"] == 3
    assert report["genuine_comparisons"] == 1
    assert report["impostor_comparisons"] == 2
    assert any(row["threshold"] == PRODUCTION_THRESHOLD and row["production_threshold"] for row in report["thresholds"])
    assert next(row for row in report["thresholds"] if row["threshold"] == 0.5)["false_accepts"] == 0
    assert next(row for row in report["thresholds"] if row["threshold"] == 0.5)["false_rejects"] == 0
    output = tmp_path / "report.csv"
    write_csv(output, report)
    assert "false_accept_rate" in output.read_text(encoding="utf-8")
    json.dumps(report)
    with pytest.raises(ValueError, match="EMPTY_DATASET"):
        evaluate_embeddings({}, [PRODUCTION_THRESHOLD])
    with pytest.raises(ValueError, match="DATASET_NOT_FOUND"):
        load_dataset(tmp_path / "missing", object())  # type: ignore[arg-type]


def test_risk_localization_is_present() -> None:
    source = Path("app/static/locales.js").read_text(encoding="utf-8")
    assert "MULTIPLE_FAILED_LOGINS:'Несколько неудачных попыток входа'" in source
    assert "MULTIPLE_FAILED_LOGINS:'Multiple failed login attempts'" in source
    assert "LOW:'Низкий'" in source and "LOW:'Low'" in source


def test_risk_is_integrated_into_admin_event_log(client: TestClient) -> None:
    signed_in = client.post(
        "/api/v3/auth/password",
        json={"login": TEST_ADMIN_NAME, "password": TEST_AUTH_INPUT},
    )
    assert signed_in.status_code == 200
    client.headers["X-CSRF-Token"] = signed_in.json()["csrf_token"]
    user_id = signed_in.json()["user_id"]
    repository = client.app.state.repository
    for _ in range(5):
        repository.add_security_event("LOGIN_FAILED", "WARNING", user_id, {"client_address": "test"})
    response = client.get("/api/v3/admin/security-events")
    assert response.status_code == 200
    row = next(item for item in response.json() if item["event_type"] == "LOGIN_FAILED")
    assert row["risk"] == {
        "risk_level": "HIGH",
        "reason_code": "MULTIPLE_FAILED_LOGINS",
        "event_count": 5,
        "window_seconds": 600,
    }


def test_openapi_is_v3_focused_and_safe(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    paths = schema["paths"]
    assert "/api/v3/auth/password" in paths
    assert "/api/v3/admin/backups" in paths
    assert "/health" in paths
    assert "/api/admin/users" not in paths
    assert "/api/verification-sessions" not in paths
    tags = {tag for path in paths.values() for operation in path.values() for tag in operation.get("tags", [])}
    assert {
        "Authentication",
        "Users",
        "Biometrics",
        "Access",
        "Evidence",
        "Sessions",
        "Admin",
        "Backups",
        "Health",
    } <= tags
    serialized = json.dumps(schema).casefold()
    for sensitive in ("password_hash", "candidate_embedding", "csrf_token", "session_secret", "filesystem_path"):
        assert sensitive not in serialized
