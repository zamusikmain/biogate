import json
from datetime import UTC, datetime
from typing import Any

from app.db.database import Database


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Repository:
    def __init__(self, database: Database):
        self.db = database

    def get_user(self, external_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE external_id=?",
                (external_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def enroll(
        self,
        external_id: str,
        display_name: str,
        model_name: str,
        model_version: str,
        embedding: bytes,
        dimensions: int,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.db.write() as conn:
            cursor = conn.execute(
                "INSERT INTO users(external_id, display_name, created_at, enrolled_at) VALUES(?,?,?,?)",
                (external_id, display_name, now, now),
            )
            user_id = int(cursor.lastrowid or 0)
            conn.execute(
                """INSERT INTO biometric_templates(
                       user_id, model_name, model_version, embedding,
                       embedding_dimensions, created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (user_id, model_name, model_version, embedding, dimensions, now),
            )
            conn.execute(
                "INSERT INTO audit_events(timestamp,event_type,user_id,result,metadata) VALUES(?,?,?,?,?)",
                (now, "BIOMETRIC_ENROLLED", user_id, "SUCCESS", json.dumps({"frames": None})),
            )
        return self.get_user(external_id) or {}

    def create_user(
        self, external_id: str, display_name: str, status: str = "ACTIVE", comment: str = ""
    ) -> dict[str, Any]:
        now = utc_now()
        with self.db.write() as conn:
            cursor = conn.execute(
                "INSERT INTO users(external_id,display_name,created_at,status,comment,updated_at) VALUES(?,?,?,?,?,?)",
                (external_id, display_name, now, status, comment, now),
            )
            user_id = int(cursor.lastrowid or 0)
            self._audit(conn, "USER_CREATED", user_id, "SUCCESS", {"status": status})
        return self.get_user(external_id) or {}

    def update_user(
        self, external_id: str, display_name: str | None = None, status: str | None = None, comment: str | None = None
    ) -> dict[str, Any] | None:
        user = self.get_user(external_id)
        if not user:
            return None
        values = {
            "display_name": display_name if display_name is not None else user["display_name"],
            "status": status if status is not None else user.get("status", "ACTIVE"),
            "comment": comment if comment is not None else user.get("comment", ""),
        }
        with self.db.write() as conn:
            conn.execute(
                "UPDATE users SET display_name=?,status=?,comment=?,updated_at=? WHERE id=?",
                (values["display_name"], values["status"], values["comment"], utc_now(), user["id"]),
            )
            event = "USER_UPDATED"
            if values["status"] != user.get("status"):
                event = "USER_BLOCKED" if values["status"] == "BLOCKED" else "USER_UNBLOCKED"
            self._audit(conn, event, int(user["id"]), "SUCCESS", {"status": values["status"]})
        return self.get_user(external_id)

    def delete_user(self, external_id: str) -> bool:
        user = self.get_user(external_id)
        if not user:
            return False
        with self.db.write() as conn:
            return bool(conn.execute("DELETE FROM users WHERE id=?", (user["id"],)).rowcount)

    def upsert_template(
        self,
        external_id: str,
        model_name: str,
        model_version: str,
        embedding: bytes,
        dimensions: int,
        sample_count: int,
    ) -> dict[str, Any]:
        user = self.get_user(external_id)
        if not user:
            raise ValueError("USER_NOT_FOUND")
        now = utc_now()
        previous = self.get_template(int(user["id"]))
        with self.db.write() as conn:
            conn.execute(
                """INSERT INTO biometric_templates(
                       user_id,model_name,model_version,embedding,embedding_dimensions,sample_count,created_at
                   ) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET model_name=excluded.model_name,
                     model_version=excluded.model_version,embedding=excluded.embedding,
                     embedding_dimensions=excluded.embedding_dimensions,sample_count=excluded.sample_count,
                     created_at=excluded.created_at""",
                (user["id"], model_name, model_version, embedding, dimensions, sample_count, now),
            )
            conn.execute("UPDATE users SET enrolled_at=?,updated_at=? WHERE id=?", (now, now, user["id"]))
            event = "BIOMETRIC_UPDATED" if previous else "BIOMETRIC_ENROLLED"
            self._audit(conn, event, int(user["id"]), "SUCCESS", {"sample_count": sample_count})
        return self.get_user(external_id) or {}

    def get_template(self, user_id: int) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM biometric_templates WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def delete_biometric(self, external_id: str) -> bool:
        user = self.get_user(external_id)
        if not user:
            return False
        now = utc_now()
        with self.db.write() as conn:
            deleted = conn.execute("DELETE FROM biometric_templates WHERE user_id=?", (user["id"],)).rowcount
            if deleted:
                conn.execute("UPDATE users SET enrolled_at=NULL,updated_at=? WHERE id=?", (now, user["id"]))
                self._audit(conn, "BIOMETRIC_DELETED", int(user["id"]), "SUCCESS", {})
        return bool(deleted)

    def add_verification(self, values: dict[str, Any]) -> int:
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        with self.db.write() as conn:
            cursor = conn.execute(
                f"INSERT INTO verification_attempts({columns}) VALUES({placeholders})",  # noqa: S608
                tuple(values.values()),
            )
            attempt_id = int(cursor.lastrowid or 0)
            event = "VERIFICATION_SUCCEEDED" if values["result"] == "VERIFIED" else "VERIFICATION_REJECTED"
            conn.execute(
                """INSERT INTO audit_events(
                   timestamp,event_type,user_id,result,metadata,attempt_id
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    values["timestamp"],
                    event,
                    values.get("user_id"),
                    values["result"],
                    json.dumps({"attempt_id": attempt_id, "reason_code": values["reason_code"]}),
                    attempt_id,
                ),
            )
        return attempt_id

    def get_verification(self, attempt_id: int) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT va.*, ats.id AS snapshot_id
                   FROM verification_attempts va
                   LEFT JOIN attempt_snapshots ats ON ats.attempt_id=va.id
                   WHERE va.id=?""",
                (attempt_id,),
            ).fetchone()
        return dict(row) if row else None

    def user_verifications(
        self, external_id: str, result: str | None = None, reason: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        sql = """SELECT va.* FROM verification_attempts va
                 JOIN users u ON u.id=va.user_id WHERE u.external_id=?"""
        params: list[Any] = [external_id]
        if result:
            sql += " AND va.result=?"
            params.append(result)
        if reason:
            sql += " AND va.reason_code=?"
            params.append(reason)
        sql += " ORDER BY va.timestamp DESC LIMIT ?"
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def verifications(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT id, claimed_external_id, timestamp, face_detected,
                          quality_score, liveness_score, similarity_score,
                          threshold, result, reason_code, inference_time_ms
                   FROM verification_attempts
                   ORDER BY timestamp DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def filtered_verifications(
        self,
        limit: int = 200,
        external_id: str | None = None,
        result: str | None = None,
        reason: str | None = None,
        liveness: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM verification_attempts WHERE 1=1"
        params: list[Any] = []
        for value, condition in (
            (external_id, "claimed_external_id=?"),
            (result, "result=?"),
            (reason, "reason_code=?"),
            (date_from, "timestamp>=?"),
            (date_to, "timestamp<=?"),
        ):
            if value:
                sql += f" AND {condition}"
                params.append(value)
        if liveness == "passed":
            sql += " AND liveness_score>0"
        elif liveness == "failed":
            sql += " AND (liveness_score=0 OR reason_code LIKE 'LIVENESS%' OR reason_code='CHALLENGE_TIMEOUT')"
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM audit_events ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

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

    def user_detail(self, external_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT u.*, bt.id AS biometric_template_id, bt.model_name,
                   bt.model_version, bt.sample_count, bt.created_at AS biometric_created_at,
                   COUNT(va.id) AS verification_attempts,
                   SUM(CASE WHEN va.result='VERIFIED' THEN 1 ELSE 0 END) AS successful,
                   SUM(CASE WHEN va.result='REJECTED' THEN 1 ELSE 0 END) AS rejected,
                   MAX(va.timestamp) AS last_verification
                   FROM users u
                   LEFT JOIN biometric_templates bt ON bt.user_id=u.id
                   LEFT JOIN verification_attempts va ON va.user_id=u.id
                   WHERE u.external_id=? GROUP BY u.id""",
                (external_id,),
            ).fetchone()
        return dict(row) if row else None

    def admin_stats(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT
                  (SELECT COUNT(*) FROM users) AS total_users,
                  (SELECT COUNT(*) FROM biometric_templates) AS enrolled_users,
                  SUM(CASE WHEN date(timestamp)=date('now') THEN 1 ELSE 0 END) AS verifications_today,
                  SUM(CASE WHEN result='VERIFIED' THEN 1 ELSE 0 END) AS successful_verifications,
                  SUM(CASE WHEN result='REJECTED' THEN 1 ELSE 0 END) AS rejected_verifications,
                  AVG(inference_time_ms) AS average_inference_time_ms,
                  AVG(CASE WHEN result='VERIFIED' THEN similarity_score END) AS average_success_similarity,
                  SUM(CASE WHEN reason_code LIKE 'LIVENESS%'
                      OR reason_code='CHALLENGE_TIMEOUT' THEN 1 ELSE 0 END) AS liveness_failures,
                  SUM(CASE WHEN reason_code IN (
                      'IMAGE_BLURRY','BAD_LIGHTING','FACE_TOO_SMALL'
                  ) THEN 1 ELSE 0 END) AS quality_failures
                  FROM verification_attempts"""
            ).fetchone()
        return dict(row) if row else {}

    def create_session(
        self, session_id: str, user_id: int, external_id: str, created_at: str, expires_at: str, sequence: list[str]
    ) -> dict[str, Any]:
        with self.db.write() as conn:
            conn.execute(
                """INSERT INTO verification_sessions(
                   id,user_id,claimed_external_id,created_at,expires_at,expected_sequence
                   ) VALUES(?,?,?,?,?,?)""",
                (session_id, user_id, external_id, created_at, expires_at, json.dumps(sequence)),
            )
        return self.get_session(session_id) or {}

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM verification_sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def update_session_progress(
        self, session_id: str, current_step: int, blink_phase: str, last_capture_at: str | None, state: str = "ACTIVE"
    ) -> None:
        with self.db.write() as conn:
            conn.execute(
                """UPDATE verification_sessions SET current_step=?,blink_phase=?,
                   last_capture_at=COALESCE(?,last_capture_at),state=? WHERE id=?""",
                (current_step, blink_phase, last_capture_at, state, session_id),
            )

    def finish_session(self, session_id: str, state: str, result: str, reason_code: str) -> None:
        with self.db.write() as conn:
            conn.execute(
                "UPDATE verification_sessions SET state=?,result=?,reason_code=?,completed_at=? WHERE id=?",
                (state, result, reason_code, utc_now(), session_id),
            )

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

    def add_snapshot(self, attempt_id: int, file_name: str, created_at: str, expires_at: str) -> None:
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO attempt_snapshots(attempt_id,file_name,created_at,expires_at) VALUES(?,?,?,?)",
                (attempt_id, file_name, created_at, expires_at),
            )

    def get_snapshot(self, attempt_id: int) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM attempt_snapshots WHERE attempt_id=?", (attempt_id,)).fetchone()
        return dict(row) if row else None

    def delete_snapshot(self, attempt_id: int) -> dict[str, Any] | None:
        snapshot = self.get_snapshot(attempt_id)
        if not snapshot:
            return None
        with self.db.write() as conn:
            conn.execute("DELETE FROM attempt_snapshots WHERE attempt_id=?", (attempt_id,))
            attempt = conn.execute("SELECT user_id FROM verification_attempts WHERE id=?", (attempt_id,)).fetchone()
            self._audit(
                conn,
                "ATTEMPT_IMAGE_DELETED",
                attempt["user_id"] if attempt else None,
                "SUCCESS",
                {"attempt_id": attempt_id},
                attempt_id,
            )
        return snapshot

    def expired_snapshots(self, now: str) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM attempt_snapshots WHERE expires_at<?", (now,))]

    def stats(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("""
                SELECT (SELECT COUNT(*) FROM biometric_templates) AS enrolled_users,
                       COUNT(*) AS total_verifications,
                       SUM(CASE WHEN result='VERIFIED' THEN 1 ELSE 0 END) AS successful_verifications,
                       SUM(CASE WHEN result='REJECTED' THEN 1 ELSE 0 END) AS rejected_verifications,
                       AVG(inference_time_ms) AS average_inference_time_ms
                FROM verification_attempts
            """).fetchone()
        return dict(row) if row else {}
