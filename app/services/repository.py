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
        samples: list[tuple[bytes, int]] | None = None,
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
            if samples:
                template = conn.execute("SELECT id FROM biometric_templates WHERE user_id=?", (user["id"],)).fetchone()
                if not template:
                    raise ValueError("BIOMETRIC_TEMPLATE_MISSING")
                conn.execute("DELETE FROM biometric_template_samples WHERE biometric_template_id=?", (template["id"],))
                conn.executemany(
                    """INSERT INTO biometric_template_samples(
                       biometric_template_id,sample_index,embedding,embedding_dimensions,created_at)
                       VALUES(?,?,?,?,?)""",
                    [
                        (template["id"], index, value, value_dimensions, now)
                        for index, (value, value_dimensions) in enumerate(samples)
                    ],
                )
            event = "BIOMETRIC_REPLACED_BY_ADMIN" if previous else "BIOMETRIC_ENROLLED"
            self._audit(conn, event, int(user["id"]), "SUCCESS", {"sample_count": sample_count})
        return self.get_user(external_id) or {}

    def get_template(self, user_id: int) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM biometric_templates WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def template_samples(self, template_id: int) -> list[dict[str, Any]]:
        """Return normalized active representatives in their deterministic storage order."""
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT embedding,embedding_dimensions,sample_index,created_at
                   FROM biometric_template_samples WHERE biometric_template_id=? ORDER BY sample_index LIMIT 5""",
                (template_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def replace_template_samples(
        self, template_id: int, samples: list[tuple[bytes, int]], *, created_at: str | None = None
    ) -> None:
        """Atomically replace a bounded representative set; callers validate selection first."""
        if not samples:
            raise ValueError("TEMPLATE_SAMPLES_REQUIRED")
        timestamp = created_at or utc_now()
        with self.db.write() as conn:
            conn.execute("DELETE FROM biometric_template_samples WHERE biometric_template_id=?", (template_id,))
            conn.executemany(
                """INSERT INTO biometric_template_samples(
                       biometric_template_id,sample_index,embedding,embedding_dimensions,created_at
                   ) VALUES(?,?,?,?,?)""",
                [
                    (template_id, index, embedding, dimensions, timestamp)
                    for index, (embedding, dimensions) in enumerate(samples)
                ],
            )

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

    def audit_events(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT ae.*,u.full_name,u.login,u.role,u.external_id FROM audit_events ae
                   LEFT JOIN users u ON u.id=ae.user_id
                   ORDER BY ae.timestamp DESC LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
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

    # BioGate V3 account and access-management persistence. These methods use
    # additive columns/tables and intentionally leave the V2 API intact.
    def get_user_by_login(self, login: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE login=?", (login,)).fetchone()
        return dict(row) if row else None

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def create_account(self, values: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.db.write() as conn:
            cursor = conn.execute(
                """INSERT INTO users(
                   external_id,display_name,created_at,status,comment,updated_at,
                   login,password_hash,full_name,employee_id,department,position,
                   role,must_change_password,password_changed_at,password_enabled,
                   face_enabled,biometric_recovery_enabled
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    values["external_id"],
                    values["full_name"],
                    now,
                    values.get("status", "ACTIVE"),
                    values.get("comment", ""),
                    now,
                    values["login"],
                    values.get("password_hash"),
                    values["full_name"],
                    values.get("employee_id"),
                    values.get("department", ""),
                    values.get("position", ""),
                    values.get("role", "USER"),
                    int(values.get("must_change_password", False)),
                    now if values.get("password_hash") else None,
                    int(values.get("password_enabled", True)),
                    int(values.get("face_enabled", True)),
                    int(values.get("biometric_recovery_enabled", True)),
                ),
            )
            user_id = int(cursor.lastrowid or 0)
            self._audit(conn, "ACCOUNT_CREATED", user_id, "SUCCESS", {"role": values.get("role", "USER")})
            conn.execute(
                """INSERT INTO notifications(user_id,audience_role,event_type,message_key,metadata,created_at)
                   VALUES(?,NULL,'ACCOUNT_CREATED','ACCOUNT_CREATED','{}',?)""",
                (user_id, now),
            )
        return self.get_user_by_id(user_id) or {}

    def list_accounts(
        self, limit: int = 50, offset: int = 0, search: str | None = None,
        status: str | None = None, role: str | None = None, biometric: str | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["1=1"]
        params: list[Any] = []
        if search:
            conditions.append("(u.full_name LIKE ? OR u.login LIKE ? OR u.employee_id LIKE ?)")
            term = f"%{search}%"
            params.extend([term, term, term])
        if status:
            conditions.append("u.status=?")
            params.append(status)
        if role:
            conditions.append("u.role=?")
            params.append(role)
        if biometric == "ENROLLED":
            conditions.append("bt.id IS NOT NULL")
        elif biometric == "NOT_ENROLLED":
            conditions.append("bt.id IS NULL")
        elif biometric == "UPDATE_PENDING":
            conditions.append(
                "EXISTS(SELECT 1 FROM biometric_requests br WHERE br.user_id=u.id "
                "AND br.status IN ('PENDING_REVIEW','REVISION_REQUIRED'))"
            )
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""SELECT u.id,u.external_id,u.login,u.full_name,u.employee_id,u.department,
                   u.position,u.role,u.status,u.must_change_password,u.created_at,u.updated_at,
                   u.last_login_at,u.last_login_method,u.password_changed_at,u.password_enabled,
                   u.face_enabled,u.biometric_recovery_enabled,bt.id AS biometric_template_id
                   FROM users u LEFT JOIN biometric_templates bt ON bt.user_id=u.id
                   WHERE {' AND '.join(conditions)} ORDER BY u.created_at DESC LIMIT ? OFFSET ?""",  # noqa: S608
                (*params, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def active_admin_count(self, excluding_user_id: int | None = None) -> int:
        sql = "SELECT COUNT(*) FROM users WHERE role='ADMIN' AND status='ACTIVE'"
        params: tuple[Any, ...] = ()
        if excluding_user_id is not None:
            sql += " AND id<>?"
            params = (excluding_user_id,)
        with self.db.connect() as conn:
            return int(conn.execute(sql, params).fetchone()[0])

    def update_account(self, user_id: int, values: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "login",
            "full_name",
            "employee_id",
            "department",
            "position",
            "role",
            "status",
            "password_enabled",
            "face_enabled",
            "biometric_recovery_enabled",
        }
        updates = {key: value for key, value in values.items() if key in allowed and value is not None}
        if not updates:
            return self.get_user_by_id(user_id)
        assignments = ",".join(f"{key}=?" for key in updates)
        with self.db.write() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                return None
            before = dict(row)
            target_role = updates.get("role", before["role"])
            target_status = updates.get("status", before["status"])
            removes_active_admin = (
                before["role"] == "ADMIN"
                and before["status"] == "ACTIVE"
                and (target_role != "ADMIN" or target_status != "ACTIVE")
            )
            if removes_active_admin:
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE role='ADMIN' AND status='ACTIVE' AND id<>?",
                    (user_id,),
                ).fetchone()[0]
                if int(remaining) == 0:
                    raise ValueError("LAST_ACTIVE_ADMIN_REQUIRED")
            target_password = bool(updates.get("password_enabled", before["password_enabled"]))
            target_face = bool(updates.get("face_enabled", before["face_enabled"]))
            if target_status == "ACTIVE" and not target_password and not target_face:
                raise ValueError("ACCOUNT_AUTH_METHOD_REQUIRED")
            conn.execute(
                # Column names are selected exclusively from the local ``allowed`` set above.
                f"UPDATE users SET {assignments},display_name=COALESCE(?,display_name),updated_at=? WHERE id=?",  # noqa: S608
                [*updates.values(), updates.get("full_name"), utc_now(), user_id],
            )
            event_type = "ACCOUNT_UPDATED"
            status = updates.get("status")
            if status != before.get("status"):
                event_type = {
                    "BLOCKED": "ACCOUNT_BLOCKED",
                    "DISABLED": "ACCOUNT_DISABLED",
                    "ACTIVE": "ACCOUNT_ENABLED" if before.get("status") == "DISABLED" else "ACCOUNT_UNBLOCKED",
                }.get(str(status), event_type)
            self._audit(
                conn, event_type, user_id, "SUCCESS",
                {"changed_fields": sorted(updates), "reason": values.get("reason", "")},
            )
        if updates.get("status") in {"BLOCKED", "DISABLED"} or (
            before.get("role") == "ADMIN" and updates.get("role") == "USER"
        ):
            self.revoke_user_sessions(user_id, "ACCOUNT_ACCESS_CHANGED")
        return self.get_user_by_id(user_id)

    def ensure_bootstrap_admin(self, login: str, password_hash: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            admin = conn.execute("SELECT * FROM users WHERE role='ADMIN' ORDER BY id LIMIT 1").fetchone()
        if admin:
            return dict(admin)
        existing = self.get_user_by_login(login) or self.get_user(login)
        if existing:
            with self.db.write() as conn:
                conn.execute(
                    """UPDATE users SET login=?,password_hash=COALESCE(password_hash,?),role='ADMIN',
                       full_name=COALESCE(NULLIF(full_name,''),display_name),updated_at=? WHERE id=?""",
                    (login, password_hash, utc_now(), existing["id"]),
                )
            return self.get_user_by_id(int(existing["id"])) or {}
        return self.create_account(
            {
                "external_id": login,
                "login": login,
                "full_name": login,
                "role": "ADMIN",
                "status": "ACTIVE",
                "password_hash": password_hash,
                "must_change_password": False,
            }
        )

    def set_password_hash(
        self,
        user_id: int,
        password_hash: str,
        must_change: bool,
        event_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        now = utc_now()
        with self.db.write() as conn:
            conn.execute(
                """UPDATE users SET password_hash=?,must_change_password=?,
                   password_changed_at=?,failed_login_count=0,locked_until=NULL,updated_at=? WHERE id=?""",
                (password_hash, int(must_change), now, now, user_id),
            )
            return self._audit(
                conn,
                event_type,
                user_id,
                "SUCCESS",
                {"must_change_password": must_change, **(metadata or {})},
            )

    def set_force_password_change(self, user_id: int, enabled: bool) -> None:
        with self.db.write() as conn:
            conn.execute(
                "UPDATE users SET must_change_password=?,updated_at=? WHERE id=?",
                (int(enabled), utc_now(), user_id),
            )
            self._audit(
                conn,
                "FORCE_PASSWORD_CHANGE_ENABLED" if enabled else "FORCE_PASSWORD_CHANGE_DISABLED",
                user_id,
                "SUCCESS",
                {},
            )

    def record_failed_login(self, user_id: int, locked_until: str | None) -> None:
        with self.db.write() as conn:
            conn.execute(
                """UPDATE users SET failed_login_count=failed_login_count+1,
                   locked_until=COALESCE(?,locked_until),updated_at=? WHERE id=?""",
                (locked_until, utc_now(), user_id),
            )

    def record_login_success(self, user_id: int, method: str | None = None) -> None:
        now = utc_now()
        with self.db.write() as conn:
            conn.execute(
                """UPDATE users SET failed_login_count=0,locked_until=NULL,
                   last_login_at=?,last_login_method=COALESCE(?,last_login_method),updated_at=? WHERE id=?""",
                (now, method, now, user_id),
            )

    def active_templates(self, *, excluding_user_id: int | None = None) -> list[dict[str, Any]]:
        sql = """SELECT bt.*,u.external_id,u.login,u.full_name,u.status,u.role
                 FROM biometric_templates bt JOIN users u ON u.id=bt.user_id
                 WHERE u.status='ACTIVE'"""
        params: list[Any] = []
        if excluding_user_id is not None:
            sql += " AND u.id<>?"
            params.append(excluding_user_id)
        with self.db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def identification_templates(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT bt.*,u.external_id,u.login,u.full_name,u.status,u.role
                   FROM biometric_templates bt JOIN users u ON u.id=bt.user_id
                   WHERE u.status IN ('ACTIVE','BLOCKED','DISABLED') AND u.face_enabled=1"""
            ).fetchall()
        return [dict(row) for row in rows]

    def create_account_session(self, values: dict[str, Any]) -> None:
        with self.db.write() as conn:
            conn.execute(
                """INSERT INTO account_sessions(
                   id,user_id,token_hash,csrf_token,created_at,expires_at,idle_expires_at,
                   last_seen_at,login_method,user_agent,client_address
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    values["id"],
                    values["user_id"],
                    values["token_hash"],
                    values["csrf_token"],
                    values["created_at"],
                    values["expires_at"],
                    values["idle_expires_at"],
                    values["last_seen_at"],
                    values["login_method"],
                    values.get("user_agent", ""),
                    values.get("client_address", ""),
                ),
            )

    def get_account_session(self, session_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT s.*,u.external_id,u.login,u.full_name,u.role,u.status,u.must_change_password
                   FROM account_sessions s JOIN users u ON u.id=s.user_id WHERE s.id=?""",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def touch_account_session(self, session_id: str, last_seen_at: str, idle_expires_at: str) -> None:
        with self.db.write() as conn:
            conn.execute(
                "UPDATE account_sessions SET last_seen_at=?,idle_expires_at=? WHERE id=? AND revoked_at IS NULL",
                (last_seen_at, idle_expires_at, session_id),
            )

    def list_account_sessions(self, user_id: int) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT id,created_at,expires_at,idle_expires_at,last_seen_at,login_method,
                   user_agent,client_address,revoked_at,revoked_reason
                   FROM account_sessions WHERE user_id=? ORDER BY created_at DESC""",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_session(self, session_id: str, reason: str, *, expected_user_id: int | None = None) -> bool:
        sql = "UPDATE account_sessions SET revoked_at=?,revoked_reason=? WHERE id=? AND revoked_at IS NULL"
        params: list[Any] = [utc_now(), reason, session_id]
        if expected_user_id is not None:
            sql += " AND user_id=?"
            params.append(expected_user_id)
        with self.db.write() as conn:
            changed = conn.execute(sql, params).rowcount
        return bool(changed)

    def revoke_user_sessions(self, user_id: int, reason: str, except_session_id: str | None = None) -> int:
        sql = "UPDATE account_sessions SET revoked_at=?,revoked_reason=? WHERE user_id=? AND revoked_at IS NULL"
        params: list[Any] = [utc_now(), reason, user_id]
        if except_session_id:
            sql += " AND id<>?"
            params.append(except_session_id)
        with self.db.write() as conn:
            return int(conn.execute(sql, params).rowcount)

    def add_access_event(self, values: dict[str, Any]) -> int:
        with self.db.write() as conn:
            cursor = conn.execute(
                """INSERT INTO access_events(
                   timestamp,user_id,method,result,reason,session_id,liveness_score,
                   similarity,second_similarity,client_address,user_agent
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    values.get("timestamp", utc_now()),
                    values.get("user_id"),
                    values["method"],
                    values["result"],
                    values["reason"],
                    values.get("session_id"),
                    values.get("liveness_score"),
                    values.get("similarity"),
                    values.get("second_similarity"),
                    values.get("client_address", ""),
                    values.get("user_agent", ""),
                ),
            )
            return int(cursor.lastrowid or 0)

    def access_events(
        self,
        *,
        user_id: int | None = None,
        limit: int = 100,
        offset: int = 0,
        search: str | None = None,
        method: str | None = None,
        result: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = """SELECT ae.*,u.external_id,u.employee_id,u.login,u.full_name,u.role,p.id AS photo_id
                 FROM access_events ae LEFT JOIN users u ON u.id=ae.user_id
                 LEFT JOIN evidence_photos p ON p.access_event_id=ae.id WHERE 1=1"""
        params: list[Any] = []
        if user_id is not None:
            sql += " AND ae.user_id=?"
            params.append(user_id)
        if search and search.strip():
            term = f"%{search.strip().casefold()}%"
            sql += " AND (LOWER(COALESCE(u.full_name,'')) LIKE ? OR LOWER(COALESCE(u.login,'')) LIKE ?)"
            params.extend((term, term))
        if method:
            sql += " AND ae.method=?"
            params.append(method)
        if result:
            if result == "DENIED_ALL":
                sql += " AND ae.result<>?"
                params.append("SUCCESS")
            else:
                sql += " AND ae.result=?"
                params.append(result)
        if date_from:
            sql += " AND substr(ae.timestamp,1,10)>=?"
            params.append(date_from)
        if date_to:
            sql += " AND substr(ae.timestamp,1,10)<=?"
            params.append(date_to)
        sql += " ORDER BY ae.timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self.db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def user_timeline(self, user_id: int, limit: int = 300) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT id,'audit' AS _category,timestamp,event_type AS event,result,metadata
                   FROM audit_events WHERE user_id=?
                   UNION ALL
                   SELECT id,'access' AS _category,timestamp,'ACCESS_' || method AS event,result,reason AS metadata
                   FROM access_events WHERE user_id=?
                   ORDER BY timestamp DESC LIMIT ?""",
                (user_id, user_id, limit),
            ).fetchall()
            timeline = [dict(row) for row in rows]
            for event in timeline:
                action = self._password_reset_action_metadata(conn, event.get("metadata"), user_id)
                if action:
                    event["action_metadata"] = action
        return timeline

    def password_reset_action_metadata(self, source_event_id: object, user_id: int | None) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            return self._password_reset_action_metadata(conn, source_event_id, user_id)

    @staticmethod
    def _password_reset_action_metadata(conn: Any, value: object, user_id: int | None) -> dict[str, Any] | None:
        metadata: object = value
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                return None
        if not isinstance(metadata, dict):
            return None
        source_id = metadata.get("source_audit_event_id")
        if not isinstance(source_id, int):
            return None
        row = conn.execute(
            """SELECT metadata FROM audit_events
               WHERE id=? AND user_id=? AND event_type='PASSWORD_RESET_BY_ADMIN'""",
            (source_id, user_id),
        ).fetchone()
        if not row:
            return None
        try:
            source = json.loads(str(row["metadata"]))
        except json.JSONDecodeError:
            return None
        if not isinstance(source, dict):
            return None
        allowed = ("reason", "administrator", "mode", "must_change_password")
        action = {key: source[key] for key in allowed if key in source}
        administrator = str(action.get("administrator") or "").strip()
        if not administrator or administrator.casefold() in {"biogate administrator", "biogate user", "admin"}:
            admin_id = source.get("admin_id")
            if isinstance(admin_id, int):
                admin = conn.execute("SELECT full_name,login FROM users WHERE id=?", (admin_id,)).fetchone()
                if admin:
                    full_name = str(admin["full_name"] or "").strip()
                    login = str(admin["login"] or "").strip()
                    action["administrator"] = (
                        full_name if full_name.casefold() not in {"biogate administrator", "biogate user"} else login
                    )
        return action

    def unknown_faces(self, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT ae.*,p.id AS photo_id,se.id AS security_event_id,se.metadata AS candidate_metadata,
                   se.reviewed_at,se.note FROM access_events ae
                   LEFT JOIN evidence_photos p ON p.access_event_id=ae.id
                   LEFT JOIN security_events se ON se.access_event_id=ae.id
                   WHERE ae.result IN ('UNKNOWN','AMBIGUOUS')
                   ORDER BY ae.timestamp DESC LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_security_event(
        self,
        event_type: str,
        severity: str,
        user_id: int | None,
        metadata: dict[str, Any],
        access_event_id: int | None = None,
    ) -> int:
        with self.db.write() as conn:
            cursor = conn.execute(
                """INSERT INTO security_events(timestamp,event_type,severity,user_id,access_event_id,metadata)
                   VALUES(?,?,?,?,?,?)""",
                (utc_now(), event_type, severity, user_id, access_event_id, json.dumps(metadata)),
            )
            self._audit(conn, event_type, user_id, severity, metadata)
            return int(cursor.lastrowid or 0)

    def security_events(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT se.*,u.full_name,u.login,u.role,u.external_id FROM security_events se
                   LEFT JOIN users u ON u.id=se.user_id
                   ORDER BY se.timestamp DESC LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def review_security_event(self, event_id: int, note: str) -> bool:
        with self.db.write() as conn:
            changed = conn.execute(
                "UPDATE security_events SET reviewed_at=?,note=? WHERE id=?",
                (utc_now(), note, event_id),
            ).rowcount
            if changed:
                self._audit(conn, "SECURITY_EVENT_REVIEWED", None, "SUCCESS", {"security_event_id": event_id})
        return bool(changed)

    def admin_notes(self, user_id: int) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT n.*,u.full_name,u.login,u.role FROM admin_notes n
                   LEFT JOIN users u ON u.id=n.author_user_id
                   WHERE n.user_id=? ORDER BY n.created_at DESC""",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_notification(
        self,
        user_id: int | None,
        audience_role: str | None,
        event_type: str,
        message_key: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self.db.write() as conn:
            cursor = conn.execute(
                """INSERT INTO notifications(user_id,audience_role,event_type,message_key,metadata,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (user_id, audience_role, event_type, message_key, json.dumps(metadata or {}), utc_now()),
            )
            return int(cursor.lastrowid or 0)

    def notifications(self, user_id: int, role: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM notifications WHERE user_id=? OR audience_role=?
                   ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                (user_id, role, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_notification_read(self, notification_id: int, user_id: int, role: str) -> bool:
        with self.db.write() as conn:
            changed = conn.execute(
                """UPDATE notifications SET read_at=? WHERE id=? AND (user_id=? OR audience_role=?)""",
                (utc_now(), notification_id, user_id, role),
            ).rowcount
        return bool(changed)

    def active_biometric_request(self, user_id: int) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT * FROM biometric_requests
                   WHERE user_id=? AND status IN ('DRAFT','PENDING_REVIEW','REVISION_REQUIRED')
                   ORDER BY created_at DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_biometric_request(
        self, values: dict[str, Any], samples: list[tuple[bytes, int]] | None = None
    ) -> dict[str, Any]:
        now = utc_now()
        with self.db.write() as conn:
            conn.execute(
                """INSERT INTO biometric_requests(
                   id,user_id,status,candidate_embedding,embedding_dimensions,model_name,model_version,
                   sample_count,duplicate_user_id,duplicate_similarity,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    values["id"],
                    values["user_id"],
                    values.get("status", "PENDING_REVIEW"),
                    values["candidate_embedding"],
                    values["embedding_dimensions"],
                    values["model_name"],
                    values["model_version"],
                    values["sample_count"],
                    values.get("duplicate_user_id"),
                    values.get("duplicate_similarity"),
                    now,
                    now,
                ),
            )
            if samples:
                conn.executemany(
                    """INSERT INTO biometric_request_samples(
                       request_id,sample_index,embedding,embedding_dimensions,created_at) VALUES(?,?,?,?,?)""",
                    [
                        (values["id"], index, embedding, dimensions, now)
                        for index, (embedding, dimensions) in enumerate(samples)
                    ],
                )
            self._audit(conn, "BIOMETRIC_UPDATE_REQUESTED", values["user_id"], "SUCCESS", {"request_id": values["id"]})
        return self.get_biometric_request(values["id"]) or {}

    def biometric_request_samples(self, request_id: str) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT embedding,embedding_dimensions,sample_index FROM biometric_request_samples
                   WHERE request_id=? ORDER BY sample_index""",
                (request_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_biometric_request(self, request_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM biometric_requests WHERE id=?", (request_id,)).fetchone()
        return dict(row) if row else None

    def biometric_request_detail(self, request_id: str) -> dict[str, Any] | None:
        """Return review-safe request data, source photos, and its own history."""
        with self.db.connect() as conn:
            request = conn.execute(
                """SELECT br.id,br.user_id,br.status,br.sample_count,br.duplicate_user_id,
                          br.duplicate_similarity,br.admin_comment,br.created_at,br.updated_at,
                          br.reviewed_by,br.reviewed_at,u.external_id,u.full_name,u.login,u.role
                   FROM biometric_requests br JOIN users u ON u.id=br.user_id
                   WHERE br.id=?""",
                (request_id,),
            ).fetchone()
            if not request:
                return None
            photos = conn.execute(
                """SELECT id,mime_type,created_at,integrity_status,size_bytes
                   FROM evidence_photos WHERE biometric_request_id=? ORDER BY created_at,id""",
                (request_id,),
            ).fetchall()
            history = conn.execute(
                """SELECT id,event_type,timestamp,metadata FROM biometric_history
                   WHERE request_id=? ORDER BY timestamp DESC,id DESC""",
                (request_id,),
            ).fetchall()
        return {
            **dict(request),
            "photos": [dict(row) for row in photos],
            "history": [dict(row) for row in history],
        }

    def biometric_requests(self, status: str | None = None, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        sql = """SELECT br.id,br.user_id,br.status,br.sample_count,br.duplicate_user_id,
                 br.duplicate_similarity,br.admin_comment,br.created_at,br.updated_at,
                 u.external_id,u.full_name,u.login,u.role FROM biometric_requests br JOIN users u ON u.id=br.user_id"""
        params: list[Any] = []
        if status:
            sql += " WHERE br.status=?"
            params.append(status)
        sql += " ORDER BY br.updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self.db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def account_biometric_summary(self, user_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            template = conn.execute(
                """SELECT id,model_name,model_version,sample_count,created_at
                   FROM biometric_templates WHERE user_id=?""",
                (user_id,),
            ).fetchone()
            request = conn.execute(
                """SELECT id,status,sample_count,duplicate_user_id,duplicate_similarity,
                   admin_comment,created_at,updated_at,reviewed_by,reviewed_at
                   FROM biometric_requests WHERE user_id=? ORDER BY updated_at DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
            history = conn.execute(
                """SELECT id,request_id,event_type,timestamp,metadata FROM biometric_history
                   WHERE user_id=? ORDER BY timestamp DESC LIMIT 100""",
                (user_id,),
            ).fetchall()
            photos = (
                conn.execute(
                    """SELECT id,mime_type,created_at,integrity_status FROM evidence_photos
                       WHERE biometric_request_id=? ORDER BY created_at""",
                    (request["id"],),
                ).fetchall()
                if request
                else []
            )
        return {
            "template": dict(template) if template else None,
            "request": dict(request) if request else None,
            "history": [dict(row) for row in history],
            "photos": [dict(row) for row in photos],
        }

    def update_biometric_request(
        self, request_id: str, status: str, admin_comment: str = "", reviewed_by: int | None = None,
        *, expected_status: str | None = None,
    ) -> bool:
        parameters = (status, admin_comment, reviewed_by, utc_now() if reviewed_by else None, utc_now(), request_id)
        with self.db.write() as conn:
            if expected_status:
                changed = conn.execute(
                    """UPDATE biometric_requests SET status=?,admin_comment=?,reviewed_by=?,reviewed_at=?,updated_at=?
                       WHERE id=? AND status=?""",
                    (*parameters, expected_status),
                ).rowcount
            else:
                changed = conn.execute(
                    """UPDATE biometric_requests SET status=?,admin_comment=?,reviewed_by=?,reviewed_at=?,updated_at=?
                       WHERE id=?""",
                    parameters,
                ).rowcount
        return bool(changed)

    def approve_biometric_request(
        self,
        request: dict[str, Any],
        admin_user_id: int | None,
        comment: str,
    ) -> None:
        """Atomically replace the active template and approve its source request."""
        now = utc_now()
        user_id = int(request["user_id"])
        with self.db.write() as conn:
            conn.execute(
                """INSERT INTO biometric_templates(
                       user_id,model_name,model_version,embedding,embedding_dimensions,sample_count,created_at
                   ) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET model_name=excluded.model_name,
                     model_version=excluded.model_version,embedding=excluded.embedding,
                     embedding_dimensions=excluded.embedding_dimensions,sample_count=excluded.sample_count,
                     created_at=excluded.created_at""",
                (
                    user_id,
                    request["model_name"],
                    request["model_version"],
                    request["candidate_embedding"],
                    request["embedding_dimensions"],
                    request["sample_count"],
                    now,
                ),
            )
            conn.execute("UPDATE users SET enrolled_at=?,updated_at=? WHERE id=?", (now, now, user_id))
            template = conn.execute("SELECT id FROM biometric_templates WHERE user_id=?", (user_id,)).fetchone()
            if not template:
                raise ValueError("BIOMETRIC_TEMPLATE_MISSING")
            samples = conn.execute(
                """SELECT embedding,embedding_dimensions FROM biometric_request_samples
                   WHERE request_id=? ORDER BY sample_index""",
                (request["id"],),
            ).fetchall()
            if not samples:
                samples = [
                    {
                        "embedding": request["candidate_embedding"],
                        "embedding_dimensions": request["embedding_dimensions"],
                    }
                ]
            conn.execute("DELETE FROM biometric_template_samples WHERE biometric_template_id=?", (template["id"],))
            if samples:
                conn.executemany(
                    """INSERT INTO biometric_template_samples(
                       biometric_template_id,sample_index,embedding,embedding_dimensions,created_at)
                       VALUES(?,?,?,?,?)""",
                    [
                        (template["id"], index, row["embedding"], row["embedding_dimensions"], now)
                        for index, row in enumerate(samples)
                    ],
                )
            changed = conn.execute(
                """UPDATE biometric_requests SET status='APPROVED',admin_comment=?,reviewed_by=?,
                   reviewed_at=?,updated_at=? WHERE id=? AND status='PENDING_REVIEW'""",
                (comment, admin_user_id, now if admin_user_id else None, now, request["id"]),
            ).rowcount
            if changed != 1:
                raise ValueError("BIOMETRIC_REQUEST_NOT_REVIEWABLE")
            conn.execute(
                """INSERT INTO biometric_history(user_id,request_id,event_type,timestamp,metadata)
                   VALUES(?,?,?,?,?)""",
                (user_id, request["id"], "BIOMETRIC_APPROVED", now, json.dumps({"comment": comment})),
            )
            self._audit(
                conn,
                "BIOMETRIC_APPROVED",
                user_id,
                "SUCCESS",
                {"request_id": request["id"]},
            )

    def add_evidence_photo(self, values: dict[str, Any]) -> None:
        with self.db.write() as conn:
            conn.execute(
                """INSERT INTO evidence_photos(
                   id,file_name,storage_state,source_type,user_id,access_event_id,biometric_request_id,
                   created_at,archived_at,mime_type,size_bytes,sha256,storage_key,integrity_status
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    values["id"],
                    values["file_name"],
                    values["storage_state"],
                    values["source_type"],
                    values.get("user_id"),
                    values.get("access_event_id"),
                    values.get("biometric_request_id"),
                    values["created_at"],
                    values.get("archived_at"),
                    values["mime_type"],
                    values["size_bytes"],
                    values["sha256"],
                    values["storage_key"],
                    values.get("integrity_status", "OK"),
                ),
            )

    def get_evidence_photo(self, photo_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM evidence_photos WHERE id=?", (photo_id,)).fetchone()
        return dict(row) if row else None

    def evidence_photos(
        self, storage_state: str | None = None, limit: int = 200, offset: int = 0
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM evidence_photos"
        params: list[Any] = []
        if storage_state:
            sql += " WHERE storage_state=?"
            params.append(storage_state)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self.db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def archive_candidates(self, cutoff: str) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM evidence_photos WHERE storage_state='ACTIVE' AND created_at<?", (cutoff,)
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_photo_archived(self, photo_id: str, storage_key: str) -> None:
        with self.db.write() as conn:
            conn.execute(
                """UPDATE evidence_photos SET storage_state='ARCHIVED',storage_key=?,archived_at=? WHERE id=?""",
                (storage_key, utc_now(), photo_id),
            )

    def set_photo_integrity(self, photo_id: str, status: str) -> None:
        with self.db.write() as conn:
            conn.execute("UPDATE evidence_photos SET integrity_status=? WHERE id=?", (status, photo_id))

    def v3_admin_stats(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT
                (SELECT COUNT(*) FROM users) total_users,
                (SELECT COUNT(*) FROM users WHERE status='ACTIVE') active_users,
                (SELECT COUNT(*) FROM users WHERE status='BLOCKED') blocked_users,
                (SELECT COUNT(*) FROM users WHERE status='DISABLED') disabled_users,
                (SELECT COUNT(*) FROM biometric_templates) enrolled_users,
                (SELECT COUNT(*) FROM access_events
                    WHERE date(timestamp)=date('now') AND result='SUCCESS') successful_today,
                (SELECT COUNT(*) FROM access_events
                    WHERE date(timestamp)=date('now') AND result<>'SUCCESS') denied_today,
                (SELECT COUNT(*) FROM access_events
                    WHERE date(timestamp)=date('now') AND result='UNKNOWN') unknown_today,
                (SELECT COUNT(*) FROM biometric_requests WHERE status='PENDING_REVIEW') pending_biometric_requests,
                (SELECT COUNT(*) FROM security_events WHERE reviewed_at IS NULL) security_alerts"""
            ).fetchone()
        return dict(row) if row else {}
