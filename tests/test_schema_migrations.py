import sqlite3
from pathlib import Path

import pytest

from app.db import database as database_module
from app.db.database import Database


def _version(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def test_initialize_enables_wal_and_records_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "biogate.db"
    Database(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert _version(path) == database_module.SCHEMA_VERSION


def test_numbered_migrations_run_once_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "biogate.db"
    Database(path).initialize()
    calls: list[int] = []

    def step(version: int) -> object:
        def apply(connection: sqlite3.Connection) -> None:
            calls.append(version)
            connection.execute(f"CREATE TABLE migration_{version}(id INTEGER)")

        return apply

    base = database_module.BASELINE_VERSION
    monkeypatch.setattr(database_module, "MIGRATIONS", {base: step(base), base + 1: step(base + 1)})
    monkeypatch.setattr(database_module, "SCHEMA_VERSION", base + 2)
    Database(path).initialize()
    Database(path).initialize()
    assert calls == [base, base + 1]
    assert _version(path) == base + 2


def test_database_from_newer_application_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "biogate.db"
    Database(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version = {database_module.SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError, match="newer"):
        Database(path).initialize()
