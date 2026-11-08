"""Biometric templates, representative samples and biometric change requests."""

import json
from typing import Any

from app.services.repository._base import RepositoryBase, utc_now


class BiometricsRepository(RepositoryBase):
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
        self,
        request_id: str,
        status: str,
        admin_comment: str = "",
        reviewed_by: int | None = None,
        *,
        expected_status: str | None = None,
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
