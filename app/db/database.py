import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  external_id TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  enrolled_at TEXT,
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  comment TEXT NOT NULL DEFAULT '',
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS biometric_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
  model_name TEXT NOT NULL,
  model_version TEXT NOT NULL,
  embedding BLOB NOT NULL,
  embedding_dimensions INTEGER NOT NULL,
  sample_count INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verification_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  claimed_external_id TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  face_detected INTEGER NOT NULL,
  quality_score REAL,
  liveness_score REAL,
  similarity_score REAL,
  threshold REAL NOT NULL,
  result TEXT NOT NULL,
  reason_code TEXT NOT NULL,
  inference_time_ms REAL NOT NULL,
  model_name TEXT,
  model_version TEXT,
  challenge_id TEXT,
  challenge_steps TEXT,
  challenge_result TEXT
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp TEXT NOT NULL,
  event_type TEXT NOT NULL,
  user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  result TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_verification_timestamp ON verification_attempts(timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_audit_timestamp ON audit_events(timestamp DESC);
CREATE TABLE IF NOT EXISTS verification_sessions (
  id TEXT PRIMARY KEY,
  user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  claimed_external_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  expected_sequence TEXT NOT NULL,
  current_step INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'ACTIVE',
  blink_phase TEXT NOT NULL DEFAULT 'WAITING_OPEN',
  last_capture_at TEXT,
  completed_at TEXT,
  result TEXT,
  reason_code TEXT
);
CREATE INDEX IF NOT EXISTS ix_sessions_expiry ON verification_sessions(expires_at);
CREATE TABLE IF NOT EXISTS attempt_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id INTEGER NOT NULL UNIQUE REFERENCES verification_attempts(id) ON DELETE CASCADE,
  file_name TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""

MIGRATION_COLUMNS: dict[str, dict[str, str]] = {
    "users": {
        "status": "TEXT NOT NULL DEFAULT 'ACTIVE'",
        "comment": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT",
    },
    "biometric_templates": {"sample_count": "INTEGER NOT NULL DEFAULT 1"},
    "verification_attempts": {
        "model_name": "TEXT",
        "model_version": "TEXT",
        "challenge_id": "TEXT",
        "challenge_steps": "TEXT",
        "challenge_result": "TEXT",
    },
    "audit_events": {"attempt_id": "INTEGER REFERENCES verification_attempts(id) ON DELETE SET NULL"},
}


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._write_lock = threading.RLock()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            for table, columns in MIGRATION_COLUMNS.items():
                existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                for column, definition in columns.items():
                    if column not in existing:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            connection.execute("PRAGMA user_version = 2")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock, self.connect() as connection:
            yield connection
