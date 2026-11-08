"""Shared state and helpers every repository part relies on."""

import json
from datetime import UTC, datetime
from typing import Any

from app.db.database import Database


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class RepositoryBase:
    def __init__(self, database: Database):
        self.db = database

    def get_user(self, external_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE external_id=?",
                (external_id,),
            ).fetchone()
        return dict(row) if row else None

    def add_audit(
        self, event_type: str, user_id: int | None, result: str, metadata: dict[str, Any], attempt_id: int | None = None
    ) -> int:
        with self.db.write() as conn:
            return self._audit(conn, event_type, user_id, result, metadata, attempt_id)

    @staticmethod
    def _audit(
        conn: Any,
        event_type: str,
        user_id: int | None,
        result: str,
        metadata: dict[str, Any],
        attempt_id: int | None = None,
    ) -> int:
        cursor = conn.execute(
            "INSERT INTO audit_events(timestamp,event_type,user_id,result,metadata,attempt_id) VALUES(?,?,?,?,?,?)",
            (utc_now(), event_type, user_id, result, json.dumps(metadata), attempt_id),
        )
        return int(cursor.lastrowid or 0)

    def get_setting(self, key: str) -> str | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.db.write() as conn:
            conn.execute(
                """INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                (key, value, utc_now()),
            )
            self._audit(conn, "SETTINGS_CHANGED", None, "SUCCESS", {"key": key, "value": value})

    def set_runtime_settings(self, values: dict[str, str], actor_user_id: int) -> None:
        now = utc_now()
        with self.db.write() as conn:
            for key, value in values.items():
                conn.execute(
                    """INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                    (key, value, now),
                )
            self._audit(
                conn,
                "SETTINGS_UPDATED",
                actor_user_id,
                "SUCCESS",
                {"changed_fields": sorted(key.removeprefix("v3_") for key in values)},
            )

    def get_user_by_login(self, login: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE login=?", (login,)).fetchone()
        return dict(row) if row else None

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None
