"""Evidence photos and their archive state."""

from typing import Any

from app.services.repository._base import RepositoryBase, utc_now


class EvidenceRepository(RepositoryBase):
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
