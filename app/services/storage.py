import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.db.database import Database
from app.services.repository import Repository

ALLOWED_EVIDENCE_MIME = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
BACKUP_FORMAT_VERSION = 1
BACKUP_SCHEMA_VERSION = 3


class EvidenceStorage:
    def __init__(self, repository: Repository, settings: Settings):
        self.repository = repository
        self.settings = settings

    def save(
        self,
        data: bytes,
        mime_type: str,
        source_type: str,
        *,
        user_id: int | None = None,
        access_event_id: int | None = None,
        biometric_request_id: str | None = None,
    ) -> dict[str, Any]:
        suffix = ALLOWED_EVIDENCE_MIME.get(mime_type)
        if not suffix or not data or len(data) > self.settings.max_image_size:
            raise ValueError("INVALID_EVIDENCE_IMAGE")
        photo_id = str(uuid.uuid4())
        file_name = f"{uuid.uuid4()}{suffix}"
        self.settings.active_image_dir.mkdir(parents=True, exist_ok=True)
        path = (self.settings.active_image_dir / file_name).resolve()
        if path.parent != self.settings.active_image_dir.resolve():
            raise ValueError("INVALID_STORAGE_PATH")
        path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        record = {
            "id": photo_id,
            "file_name": file_name,
            "storage_state": "ACTIVE",
            "source_type": source_type,
            "user_id": user_id,
            "access_event_id": access_event_id,
            "biometric_request_id": biometric_request_id,
            "created_at": datetime.now(UTC).isoformat(),
            "mime_type": mime_type,
            "size_bytes": len(data),
            "sha256": digest,
            "storage_key": file_name,
        }
        self.repository.add_evidence_photo(record)
        return record

    def resolve(self, record: dict[str, Any]) -> Path:
        root = self.settings.archive_dir if record["storage_state"] == "ARCHIVED" else self.settings.active_image_dir
        path = (root / str(record["storage_key"])).resolve()
        if path.parent != root.resolve() or not path.is_file():
            raise FileNotFoundError("EVIDENCE_NOT_FOUND")
        return path

    def archive_due(self, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        cutoff = (current - timedelta(days=self.settings.active_image_days)).isoformat()
        self.settings.archive_dir.mkdir(parents=True, exist_ok=True)
        moved = 0
        for record in self.repository.archive_candidates(cutoff):
            source = self.resolve(record)
            destination = (self.settings.archive_dir / str(record["file_name"])).resolve()
            if destination.parent != self.settings.archive_dir.resolve():
                continue
            if destination.exists():
                if hashlib.sha256(destination.read_bytes()).hexdigest() != record["sha256"]:
                    self._integrity_failed(record)
                    continue
                source.unlink(missing_ok=True)
            else:
                shutil.move(str(source), str(destination))
            self.repository.mark_photo_archived(str(record["id"]), destination.name)
            self.repository.add_audit("PHOTO_ARCHIVED", record.get("user_id"), "SUCCESS", {"photo_id": record["id"]})
            moved += 1
        return moved

    def verify_integrity(self, photo_id: str) -> bool:
        record = self.repository.get_evidence_photo(photo_id)
        if not record:
            raise FileNotFoundError("EVIDENCE_NOT_FOUND")
        try:
            path = self.resolve(record)
            valid = hashlib.sha256(path.read_bytes()).hexdigest() == str(record["sha256"])
        except FileNotFoundError:
            valid = False
        if not valid:
            self._integrity_failed(record)
        else:
            self.repository.set_photo_integrity(photo_id, "OK")
        return bool(valid)

    def _integrity_failed(self, record: dict[str, Any]) -> None:
        if record.get("integrity_status") == "INTEGRITY_FAILED":
            return
        self.repository.set_photo_integrity(str(record["id"]), "INTEGRITY_FAILED")
        metadata = {"photo_id": record["id"], "storage_state": record["storage_state"]}
        self.repository.add_security_event("ARCHIVE_INTEGRITY_FAILED", "CRITICAL", record.get("user_id"), metadata)


class BackupService:
    def __init__(self, repository: Repository, settings: Settings):
        self.repository = repository
        self.settings = settings

    def create(self, *, kind: str = "USER", audit: bool = True) -> dict[str, Any]:
        backup_id = str(uuid.uuid4())
        self.settings.backup_dir.mkdir(parents=True, exist_ok=True)
        filename = f"biogate-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{backup_id[:8]}.zip"
        destination = self.settings.backup_dir / filename
        database_copy = self.settings.backup_dir / f"{backup_id}.sqlite"
        source = sqlite3.connect(self.settings.db_path)
        target = sqlite3.connect(database_copy)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        try:
            included: list[tuple[Path, str]] = [(database_copy, "database/biogate.db")]
            for root, prefix in (
                (self.settings.active_image_dir, "active_images"),
                (self.settings.archive_dir, "archive"),
            ):
                if root.exists():
                    included.extend(
                        (path, f"{prefix}/{path.relative_to(root).as_posix()}")
                        for path in sorted(root.rglob("*"))
                        if path.is_file()
                    )
            manifest = {
                "backup_format_version": BACKUP_FORMAT_VERSION,
                "schema_version": BACKUP_SCHEMA_VERSION,
                "created_at": datetime.now(UTC).isoformat(),
                "kind": kind,
                "files": [
                    {"path": name, "sha256": self._file_sha256(path), "size_bytes": path.stat().st_size}
                    for path, name in included
                ],
            }
            with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path, name in included:
                    archive.write(path, name)
                archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True, separators=(",", ":")))
        finally:
            database_copy.unlink(missing_ok=True)
        data = destination.read_bytes()
        record = {
            "id": backup_id,
            "created_at": datetime.now(UTC).isoformat(),
            "file_name": filename,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "validation_status": "VALID",
            "kind": kind,
        }
        with self.repository.db.write() as conn:
            conn.execute(
                """INSERT INTO backups(id,created_at,file_name,size_bytes,sha256,validation_status,kind)
                   VALUES(?,?,?,?,?,?,?)""",
                tuple(record.values()),
            )
        if audit:
            self.repository.add_audit("BACKUP_CREATED", None, "SUCCESS", {"backup_id": backup_id})
        return record

    def validate(self, backup_id: str) -> bool:
        row = self._record(backup_id)
        if not row:
            return False
        path = (self.settings.backup_dir / row["file_name"]).resolve()
        if path.parent != self.settings.backup_dir.resolve() or not path.is_file():
            return False
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            return False
        try:
            self._validate_archive(path)
        except (ValueError, OSError, zipfile.BadZipFile):
            return False
        return True

    def list(self) -> list[dict[str, Any]]:
        with self.repository.db.connect() as conn:
            rows = conn.execute("SELECT * FROM backups WHERE kind='USER' ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def delete(self, backup_id: str) -> bool:
        record = self._record(backup_id)
        if not record:
            return False
        path = (self.settings.backup_dir / str(record["file_name"])).resolve()
        if path.parent != self.settings.backup_dir.resolve():
            raise ValueError("INVALID_BACKUP_PATH")
        path.unlink(missing_ok=True)
        with self.repository.db.write() as conn:
            conn.execute("DELETE FROM backups WHERE id=?", (backup_id,))
        return True

    def restore(self, backup_id: str) -> dict[str, Any]:
        record = self._record(backup_id)
        if not record:
            raise ValueError("BACKUP_VALIDATION_FAILED")
        source = (self.settings.backup_dir / str(record["file_name"])).resolve()
        if source.parent != self.settings.backup_dir.resolve():
            raise ValueError("INVALID_BACKUP_PATH")
        if hashlib.sha256(source.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError("BACKUP_INTEGRITY_FAILED")
        self._validate_archive(source)
        safety = self.create(kind="SAFETY", audit=False)
        with tempfile.TemporaryDirectory(prefix="biogate-restore-") as temporary:
            temporary_root = Path(temporary)
            restored_database = temporary_root / "biogate.db"
            with zipfile.ZipFile(source) as archive:
                if archive.testzip() is not None:
                    raise ValueError("BACKUP_VALIDATION_FAILED")
                try:
                    database_info = archive.getinfo("database/biogate.db")
                except KeyError as error:
                    raise ValueError("BACKUP_DATABASE_MISSING") from error
                with archive.open(database_info) as source_db, restored_database.open("wb") as target_db:
                    shutil.copyfileobj(source_db, target_db)
                self._validate_database(restored_database)
                self._restore_evidence(archive, "active_images/", self.settings.active_image_dir)
                self._restore_evidence(archive, "archive/", self.settings.archive_dir)
            replacement = self.settings.db_path.with_suffix(".restore.tmp")
            shutil.copy2(restored_database, replacement)
            for sidecar in (
                Path(f"{self.settings.db_path}-wal"),
                Path(f"{self.settings.db_path}-shm"),
            ):
                sidecar.unlink(missing_ok=True)
            os.replace(replacement, self.settings.db_path)
        Database(self.settings.db_path).initialize()
        self._remember_backup(record)
        self._remember_backup(safety)
        self.repository.add_audit(
            "BACKUP_RESTORED",
            None,
            "SUCCESS",
            {"backup_id": backup_id, "safety_backup_id": safety["id"]},
        )
        return {"restored": True, "backup_id": backup_id, "safety_backup_id": safety["id"]}

    def _record(self, backup_id: str) -> dict[str, Any] | None:
        with self.repository.db.connect() as conn:
            row = conn.execute("SELECT * FROM backups WHERE id=?", (backup_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _validate_database(path: Path) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("BACKUP_DATABASE_INTEGRITY_FAILED")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.DatabaseError as error:
            raise ValueError("BACKUP_DATABASE_INVALID") from error
        finally:
            if connection is not None:
                connection.close()
        if not {"users", "biometric_templates", "audit_events"} <= tables:
            raise ValueError("BACKUP_DATABASE_SCHEMA_INVALID")
        if schema_version != BACKUP_SCHEMA_VERSION:
            raise ValueError("BACKUP_SCHEMA_INCOMPATIBLE")

    def _validate_archive(self, path: Path) -> dict[str, Any]:
        if not zipfile.is_zipfile(path):
            raise ValueError("BACKUP_ARCHIVE_INVALID")
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise ValueError("BACKUP_INTEGRITY_FAILED")
            try:
                raw_manifest = archive.read("manifest.json")
            except KeyError as error:
                raise ValueError("BACKUP_LEGACY_FORMAT") from error
            try:
                manifest = json.loads(raw_manifest)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
                raise ValueError("BACKUP_MANIFEST_INVALID") from error
            if not isinstance(manifest, dict):
                raise ValueError("BACKUP_MANIFEST_INVALID")
            if manifest.get("backup_format_version") != BACKUP_FORMAT_VERSION:
                raise ValueError("BACKUP_VERSION_UNSUPPORTED")
            if manifest.get("schema_version") != BACKUP_SCHEMA_VERSION:
                raise ValueError("BACKUP_SCHEMA_INCOMPATIBLE")
            if manifest.get("kind") not in {"USER", "SAFETY"} or not isinstance(manifest.get("created_at"), str):
                raise ValueError("BACKUP_MANIFEST_INVALID")
            files = manifest.get("files")
            if not isinstance(files, list):
                raise ValueError("BACKUP_MANIFEST_INVALID")
            expected: dict[str, dict[str, Any]] = {}
            for item in files:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    raise ValueError("BACKUP_MANIFEST_INVALID")
                name = str(item["path"])
                if name in expected or name == "manifest.json" or Path(name).is_absolute() or ".." in Path(name).parts:
                    raise ValueError("BACKUP_MANIFEST_INVALID")
                expected[name] = item
            if "database/biogate.db" not in expected:
                raise ValueError("BACKUP_DATABASE_MISSING")
            actual_names = [
                info.filename for info in archive.infolist() if not info.is_dir() and info.filename != "manifest.json"
            ]
            actual = set(actual_names)
            if len(actual_names) != len(actual) or actual != set(expected):
                raise ValueError("BACKUP_MANIFEST_FILE_MISMATCH")
            for name, item in expected.items():
                data = archive.read(name)
                if item.get("size_bytes") != len(data) or item.get("sha256") != hashlib.sha256(data).hexdigest():
                    raise ValueError("BACKUP_INTEGRITY_FAILED")
            with tempfile.TemporaryDirectory(prefix="biogate-backup-validate-") as temporary:
                database_path = Path(temporary) / "biogate.db"
                database_path.write_bytes(archive.read("database/biogate.db"))
                self._validate_database(database_path)
            return manifest

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _restore_evidence(archive: zipfile.ZipFile, prefix: str, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        for info in archive.infolist():
            if info.is_dir() or not info.filename.startswith(prefix):
                continue
            relative = Path(info.filename.removeprefix(prefix))
            target = (destination / relative).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ValueError("BACKUP_PATH_TRAVERSAL")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source_file, target.open("wb") as target_file:
                shutil.copyfileobj(source_file, target_file)

    def _remember_backup(self, record: dict[str, Any]) -> None:
        with self.repository.db.write() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO backups(id,created_at,file_name,size_bytes,sha256,validation_status,kind)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    record["id"],
                    record["created_at"],
                    record["file_name"],
                    record["size_bytes"],
                    record["sha256"],
                    record["validation_status"],
                    record.get("kind", "USER"),
                ),
            )
