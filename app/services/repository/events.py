"""Access, audit and security events, notifications and legacy V2 attempt history."""

import json
from typing import Any

from app.services.repository._base import RepositoryBase, utc_now


class EventsRepository(RepositoryBase):
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
        after_id: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = """SELECT ae.*,u.external_id,u.employee_id,u.login,u.full_name,u.role,p.id AS photo_id
                 FROM access_events ae LEFT JOIN users u ON u.id=ae.user_id
                 LEFT JOIN evidence_photos p ON p.access_event_id=ae.id WHERE 1=1"""
        params: list[Any] = []
        if after_id is not None:
            sql += " AND ae.id>?"
            params.append(after_id)
        if user_id is not None:
            sql += " AND ae.user_id=?"
            params.append(user_id)
        if search and search.strip():
            term = f"%{search.strip().casefold()}%"
            sql += " AND (casefold(COALESCE(u.full_name,'')) LIKE ? OR casefold(COALESCE(u.login,'')) LIKE ?)"
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
