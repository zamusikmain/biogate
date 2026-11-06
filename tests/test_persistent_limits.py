"""Lockouts and attempt counters are stored in SQLite and survive a restart."""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.db.database import Database
from app.main import create_app
from app.services.rate_limit import PASSWORD_FAILURES, RETENTION_SECONDS, RateLimitStore
from tests.conftest import TEST_ADMIN_NAME, TEST_AUTH_INPUT, FakePipeline


@contextmanager
def restarted(client: TestClient) -> Iterator[TestClient]:
    """A fresh application process on the same database and settings."""
    with TestClient(create_app(client.app.state.settings, FakePipeline())) as fresh:  # type: ignore[arg-type]
        yield fresh


def test_password_lockout_survives_restart(client: TestClient) -> None:
    attempts = client.app.state.settings.password_login_max_attempts
    for _ in range(attempts):
        failed = client.post("/api/v3/auth/password", json={"login": "nobody", "password": "Wrong-password-1"})
        assert failed.json()["detail"]["reason_code"] == "INVALID_CREDENTIALS"
    with restarted(client) as fresh:
        limited = fresh.post("/api/v3/auth/password", json={"login": "nobody", "password": "Wrong-password-1"})
        assert limited.status_code == 429
        assert limited.json()["detail"]["reason_code"] == "LOGIN_RATE_LIMITED"


def test_successful_password_login_clears_stored_failures(client: TestClient) -> None:
    for _ in range(2):
        client.post("/api/v3/auth/password", json={"login": TEST_ADMIN_NAME, "password": "Wrong-password-1"})
    with client.app.state.repository.db.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM rate_limit_events WHERE bucket=?", (PASSWORD_FAILURES,)
            ).fetchone()[0]
            == 2
        )
    ok = client.post("/api/v3/auth/password", json={"login": TEST_ADMIN_NAME, "password": TEST_AUTH_INPUT})
    assert ok.status_code == 200
    with client.app.state.repository.db.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM rate_limit_events WHERE bucket=?", (PASSWORD_FAILURES,)
            ).fetchone()[0]
            == 0
        )


def test_face_challenge_limit_survives_restart(client: TestClient) -> None:
    for _ in range(client.app.state.settings.face_login_max_attempts):
        assert client.post("/api/v3/face/challenges").status_code == 200
    with restarted(client) as fresh:
        limited = fresh.post("/api/v3/face/challenges")
        assert limited.status_code == 429
        assert limited.json()["detail"]["reason_code"] == "FACE_LOGIN_RATE_LIMITED"


def test_recovery_limit_survives_restart(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = client.app.state.settings
    # Keep the per-client face limit out of the way so only the recovery limit applies.
    monkeypatch.setattr(settings, "face_login_max_attempts", 100)
    for _ in range(settings.recovery_max_attempts):
        assert client.post("/api/v3/recovery/challenges", json={"login": "target"}).status_code == 200
    with restarted(client) as fresh:
        limited = fresh.post("/api/v3/recovery/challenges", json={"login": "target"})
        assert limited.status_code == 429
        assert limited.json()["detail"]["reason_code"] == "RECOVERY_RATE_LIMITED"


def test_counters_store_no_plaintext_login_or_address(client: TestClient) -> None:
    client.post("/api/v3/auth/password", json={"login": "secret-login", "password": "Wrong-password-1"})
    client.post("/api/v3/face/challenges")
    client.post("/api/v3/recovery/challenges", json={"login": "secret-login"})
    with client.app.state.repository.db.connect() as connection:
        keys = [row[0] for row in connection.execute("SELECT key FROM rate_limit_events")]
    assert len(keys) == 4
    assert not any("secret-login" in key or "testclient" in key for key in keys)


def test_old_rows_are_pruned(tmp_path) -> None:  # type: ignore[no-untyped-def]
    database = Database(tmp_path / "limits.db")
    database.initialize()
    store = RateLimitStore(database)
    store.record("TEST", "k", now=1_000.0)
    store.record("TEST", "k", now=1_000.0 + RETENTION_SECONDS + 120)
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM rate_limit_events").fetchone()[0] == 1


def test_record_if_under_and_clear_prefix(tmp_path) -> None:  # type: ignore[no-untyped-def]
    database = Database(tmp_path / "limits.db")
    database.initialize()
    store = RateLimitStore(database)
    assert store.record_if_under("TEST", "ab:1", 2, 60, now=100.0)
    assert store.record_if_under("TEST", "ab:1", 2, 60, now=101.0)
    assert not store.record_if_under("TEST", "ab:1", 2, 60, now=102.0)
    assert store.record_if_under("TEST", "ab:1", 2, 60, now=200.0)
    store.record("TEST", "ab:2", now=200.0)
    store.record("TEST", "cd:1", now=200.0)
    store.clear_prefix("TEST", "ab:")
    assert store.stats("TEST", "ab:1", 1_000, now=200.0).count == 0
    assert store.stats("TEST", "cd:1", 1_000, now=200.0).count == 1
