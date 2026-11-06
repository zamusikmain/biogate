"""Automatic archiving, snapshot cleanup, disk check and log rotation."""

import asyncio
import json
import logging
import logging.handlers
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.logging import configure_logging
from app.main import create_app
from app.services import maintenance
from app.services.maintenance import disk_status, maintenance_loop, run_maintenance
from tests.conftest import FakePipeline


def _old_photo(client: TestClient) -> dict[str, object]:
    record = client.app.state.evidence_storage.save(b"jpeg-bytes", "image/jpeg", "TEST")
    old = (datetime.now(UTC) - timedelta(days=client.app.state.settings.active_image_days + 1)).isoformat()
    with client.app.state.repository.db.write() as connection:
        connection.execute("UPDATE evidence_photos SET created_at=? WHERE id=?", (old, record["id"]))
    return record


def _state(client: TestClient, photo_id: object) -> str:
    with client.app.state.repository.db.connect() as connection:
        row = connection.execute("SELECT storage_state FROM evidence_photos WHERE id=?", (photo_id,)).fetchone()
    return str(row[0])


def test_maintenance_archives_old_photos(client: TestClient) -> None:
    record = _old_photo(client)
    fresh = client.app.state.evidence_storage.save(b"fresh-bytes", "image/jpeg", "TEST")
    app = client.app
    result = run_maintenance(app.state.repository, app.state.evidence_storage, app.state.settings)
    assert result["archived"] == 1 and result["disk_low"] is False
    assert _state(client, record["id"]) == "ARCHIVED"
    assert _state(client, fresh["id"]) == "ACTIVE"


def test_maintenance_runs_at_startup(client: TestClient) -> None:
    record = _old_photo(client)
    with TestClient(create_app(client.app.state.settings, FakePipeline())):  # type: ignore[arg-type]
        pass
    assert _state(client, record["id"]) == "ARCHIVED"


def test_low_disk_space_is_reported(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = client.app.state.settings
    monkeypatch.setattr(settings, "min_free_disk_mb", 1024 * 1024)
    assert disk_status(settings)["low"] is True
    result = run_maintenance(client.app.state.repository, client.app.state.evidence_storage, settings)
    assert result["disk_low"] is True
    events = [row["event_type"] for row in client.app.state.repository.security_events(50)]
    assert "LOW_DISK_SPACE" in events
    health = client.get("/api/v3/admin/system-health").json()
    assert health["components"]["disk"] == "WARNING"
    assert health["disk"]["free_bytes"] > 0


def test_system_health_reports_disk_ok_above_threshold(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert client.app.state.settings.min_free_disk_mb == 1024
    monkeypatch.setattr(client.app.state.settings, "min_free_disk_mb", 0)
    health = client.get("/api/v3/admin/system-health").json()
    assert health["components"]["disk"] == "OK"
    assert health["disk"]["threshold_bytes"] == 0


def test_maintenance_loop_repeats_and_survives_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def flaky(*_: object) -> dict[str, object]:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {}

    monkeypatch.setattr(maintenance, "run_maintenance", flaky)
    settings = Settings(db_path=tmp_path / "loop.db", maintenance_interval_seconds=0)
    object.__setattr__(settings, "maintenance_interval_seconds", 0.01)

    async def scenario() -> None:
        task = asyncio.create_task(maintenance_loop(None, None, settings))  # type: ignore[arg-type]
        while len(calls) < 3:
            await asyncio.sleep(0.01)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    assert len(calls) >= 3


def test_rotating_log_file(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "biogate.log"
    try:
        configure_logging(log_file=log_file, max_bytes=64 * 1024, backup_count=2)
        logging.getLogger("biogate.test").info("hello", extra={"operation": "test"})
        for handler in logging.getLogger().handlers:
            handler.flush()
        line = log_file.read_text(encoding="utf-8").strip().splitlines()[-1]
        assert json.loads(line)["message"] == "hello"
        rotating = [h for h in logging.getLogger().handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert rotating and rotating[0].backupCount == 2 and rotating[0].maxBytes == 64 * 1024
    finally:
        configure_logging()
