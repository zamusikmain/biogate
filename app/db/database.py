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
CREATE TABLE IF NOT EXISTS biometric_template_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  biometric_template_id INTEGER NOT NULL REFERENCES biometric_templates(id) ON DELETE CASCADE,
  sample_index INTEGER NOT NULL,
  embedding BLOB NOT NULL,
  embedding_dimensions INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(biometric_template_id, sample_index)
);
CREATE INDEX IF NOT EXISTS ix_biometric_template_samples_template
ON biometric_template_samples(biometric_template_id, sample_index);
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
CREATE TABLE IF NOT EXISTS account_sessions (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  csrf_token TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  idle_expires_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  login_method TEXT NOT NULL,
  user_agent TEXT NOT NULL DEFAULT '',
  client_address TEXT NOT NULL DEFAULT '',
  revoked_at TEXT,
  revoked_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_account_sessions_user ON account_sessions(user_id, revoked_at);
CREATE TABLE IF NOT EXISTS access_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp TEXT NOT NULL,
  user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  method TEXT NOT NULL,
  result TEXT NOT NULL,
  reason TEXT NOT NULL,
  session_id TEXT REFERENCES account_sessions(id) ON DELETE SET NULL,
  liveness_score REAL,
  similarity REAL,
  second_similarity REAL,
  client_address TEXT NOT NULL DEFAULT '',
  user_agent TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_access_events_time ON access_events(timestamp DESC);
CREATE INDEX IF NOT EXISTS ix_access_events_user ON access_events(user_id, timestamp DESC);
CREATE TABLE IF NOT EXISTS evidence_photos (
  id TEXT PRIMARY KEY,
  file_name TEXT NOT NULL UNIQUE,
  storage_state TEXT NOT NULL,
  source_type TEXT NOT NULL,
  user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  access_event_id INTEGER REFERENCES access_events(id) ON DELETE SET NULL,
  biometric_request_id TEXT,
  created_at TEXT NOT NULL,
  archived_at TEXT,
  mime_type TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  storage_key TEXT NOT NULL UNIQUE,
  integrity_status TEXT NOT NULL DEFAULT 'OK'
);
CREATE INDEX IF NOT EXISTS ix_evidence_created ON evidence_photos(created_at DESC);
CREATE TABLE IF NOT EXISTS biometric_requests (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  candidate_embedding BLOB NOT NULL,
  embedding_dimensions INTEGER NOT NULL,
  model_name TEXT NOT NULL,
  model_version TEXT NOT NULL,
  sample_count INTEGER NOT NULL,
  duplicate_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  duplicate_similarity REAL,
  admin_comment TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  reviewed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS biometric_request_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL REFERENCES biometric_requests(id) ON DELETE CASCADE,
  sample_index INTEGER NOT NULL,
  embedding BLOB NOT NULL,
  embedding_dimensions INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(request_id, sample_index)
);
CREATE INDEX IF NOT EXISTS ix_biometric_request_samples_request
ON biometric_request_samples(request_id, sample_index);
CREATE INDEX IF NOT EXISTS ix_biometric_requests_user ON biometric_requests(user_id, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ux_biometric_requests_active_user ON biometric_requests(user_id)
WHERE status IN ('DRAFT','PENDING_REVIEW','REVISION_REQUIRED');
CREATE TABLE IF NOT EXISTS biometric_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  request_id TEXT REFERENCES biometric_requests(id) ON DELETE SET NULL,
  event_type TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
  audience_role TEXT,
  event_type TEXT NOT NULL,
  message_key TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  read_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_notifications_user ON notifications(user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS security_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp TEXT NOT NULL,
  event_type TEXT NOT NULL,
  severity TEXT NOT NULL,
  user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  access_event_id INTEGER REFERENCES access_events(id) ON DELETE SET NULL,
  metadata TEXT NOT NULL DEFAULT '{}',
  reviewed_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_security_events_time ON security_events(timestamp DESC);
CREATE TABLE IF NOT EXISTS password_reset_authorizations (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);
CREATE TABLE IF NOT EXISTS admin_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  author_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  text TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backups (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  file_name TEXT NOT NULL UNIQUE,
  size_bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  validation_status TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'USER'
);
"""

MIGRATION_COLUMNS: dict[str, dict[str, str]] = {
    "users": {
        "status": "TEXT NOT NULL DEFAULT 'ACTIVE'",
        "comment": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT",
        "login": "TEXT",
        "password_hash": "TEXT",
        "full_name": "TEXT",
        "employee_id": "TEXT",
        "department": "TEXT NOT NULL DEFAULT ''",
        "position": "TEXT NOT NULL DEFAULT ''",
        "role": "TEXT NOT NULL DEFAULT 'USER'",
        "must_change_password": "INTEGER NOT NULL DEFAULT 0",
        "last_login_at": "TEXT",
        "password_changed_at": "TEXT",
        "failed_login_count": "INTEGER NOT NULL DEFAULT 0",
        "locked_until": "TEXT",
        "last_login_method": "TEXT",
        "password_enabled": "INTEGER NOT NULL DEFAULT 1",
        "face_enabled": "INTEGER NOT NULL DEFAULT 1",
        "biometric_recovery_enabled": "INTEGER NOT NULL DEFAULT 1",
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
    "backups": {"kind": "TEXT NOT NULL DEFAULT 'USER'"},
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
            connection.execute("UPDATE users SET login=external_id WHERE login IS NULL OR login='' ")
            connection.execute("UPDATE users SET full_name=display_name WHERE full_name IS NULL OR full_name='' ")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_users_login ON users(login)")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_employee_id "
                "ON users(employee_id) WHERE employee_id IS NOT NULL"
            )
            connection.execute(
                """INSERT INTO biometric_template_samples(
                       biometric_template_id,sample_index,embedding,embedding_dimensions,created_at
                   )
                   SELECT id,0,embedding,embedding_dimensions,created_at FROM biometric_templates
                   WHERE NOT EXISTS (
                       SELECT 1 FROM biometric_template_samples sample
                       WHERE sample.biometric_template_id=biometric_templates.id
                   )"""
            )
            connection.execute("PRAGMA user_version = 3")

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
