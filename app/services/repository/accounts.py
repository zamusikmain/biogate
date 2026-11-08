"""Accounts, passwords, login sessions, admin notes and account erasure."""

import json
from typing import Any

from app.services.repository._base import RepositoryBase, utc_now


class AccountsRepository(RepositoryBase):
    def list_users(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

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
        self,
        limit: int = 50,
        offset: int = 0,
        search: str | None = None,
        status: str | None = None,
        role: str | None = None,
        biometric: str | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["1=1"]
        params: list[Any] = []
        if search:
            conditions.append(
                "(casefold(u.full_name) LIKE ? OR casefold(u.login) LIKE ? OR casefold(u.employee_id) LIKE ?)"
            )
            term = f"%{search.strip().casefold()}%"
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
                   WHERE {" AND ".join(conditions)} ORDER BY u.created_at DESC LIMIT ? OFFSET ?""",  # noqa: S608
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
                conn,
                event_type,
                user_id,
                "SUCCESS",
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

    def admin_notes(self, user_id: int) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT n.*,u.full_name,u.login,u.role FROM admin_notes n
                   LEFT JOIN users u ON u.id=n.author_user_id
                   WHERE n.user_id=? ORDER BY n.created_at DESC""",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def erase_account(
        self, user_id: int, *, erasure_id: str, admin_id: int | None, reason: str
    ) -> dict[str, Any] | None:
        """Remove a person and every biometric artefact; keep only pseudonymous event skeletons.

        Returns the evidence and snapshot file names the caller must remove from disk once the
        transaction has committed.
        """
        with self.db.write() as conn:
            conn.execute("PRAGMA secure_delete = ON")
            user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not user:
                return None
            if user["role"] == "ADMIN" and user["status"] == "ACTIVE":
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE role='ADMIN' AND status='ACTIVE' AND id<>?", (user_id,)
                ).fetchone()[0]
                if int(remaining) == 0:
                    raise ValueError("LAST_ACTIVE_ADMIN_REQUIRED")
            photos = [
                dict(row)
                for row in conn.execute(
                    """SELECT id,storage_state,storage_key FROM evidence_photos
                       WHERE user_id=:user
                          OR access_event_id IN (SELECT id FROM access_events WHERE user_id=:user)
                          OR biometric_request_id IN (SELECT id FROM biometric_requests WHERE user_id=:user)""",
                    {"user": user_id},
                ).fetchall()
            ]
            snapshots = [
                str(row["file_name"])
                for row in conn.execute(
                    """SELECT s.file_name FROM attempt_snapshots s
                       JOIN verification_attempts va ON va.id=s.attempt_id WHERE va.user_id=?""",
                    (user_id,),
                ).fetchall()
            ]
            conn.executemany("DELETE FROM evidence_photos WHERE id=?", [(photo["id"],) for photo in photos])
            conn.execute(
                """DELETE FROM attempt_snapshots
                   WHERE attempt_id IN (SELECT id FROM verification_attempts WHERE user_id=?)""",
                (user_id,),
            )
            # Event rows survive as pseudonymous counters: identifiers, network metadata and free text go.
            erased = json.dumps({"erased_subject": True, "erasure_id": erasure_id})
            conn.execute("UPDATE verification_attempts SET claimed_external_id='ERASED' WHERE user_id=?", (user_id,))
            conn.execute("UPDATE verification_sessions SET claimed_external_id='ERASED' WHERE user_id=?", (user_id,))
            conn.execute("UPDATE access_events SET client_address='',user_agent='' WHERE user_id=?", (user_id,))
            conn.execute("UPDATE audit_events SET metadata=? WHERE user_id=?", (erased, user_id))
            conn.execute("UPDATE security_events SET metadata=?,note='' WHERE user_id=?", (erased, user_id))
            for key in ("best_user_id", "second_user_id", "duplicate_user_id"):
                conn.execute(
                    f"UPDATE security_events SET metadata=json_set(metadata,'$.{key}',NULL) "  # noqa: S608
                    f"WHERE json_valid(metadata) AND json_extract(metadata,'$.{key}')=?",
                    (user_id,),
                )
            # Cascades remove templates, samples, requests, history, sessions, notifications and notes;
            # remaining references become NULL.
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            self._audit(
                conn,
                "ACCOUNT_ERASED",
                None,
                "SUCCESS",
                {
                    "erasure_id": erasure_id,
                    "admin_id": admin_id,
                    "reason": reason,
                    "evidence_photos": len(photos),
                    "attempt_snapshots": len(snapshots),
                },
            )
        with self.db.connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"photos": photos, "snapshots": snapshots}
