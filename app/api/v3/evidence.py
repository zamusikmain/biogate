"""Protected evidence photos, the archive and backups."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from app.api.dependencies import (
    get_backup_service,
    get_evidence_storage,
    get_repository,
    require_account_admin,
    require_full_account_session,
)
from app.schemas.v3 import (
    AdminReasonRequest,
    BackupRestoreRequest,
)
from app.services.account_auth import AccountSession
from app.services.repository import Repository
from app.services.storage import BackupService, EvidenceStorage

router = APIRouter()


@router.get("/admin/archive")
def archive_list(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
    storage_state: str | None = None,
    user_id: int | None = None,
    source: str | None = None,
    integrity: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    return [
        row
        for row in repository.evidence_photos(storage_state, limit, offset)
        if (user_id is None or row["user_id"] == user_id)
        and (not source or row["source_type"] == source)
        and (not integrity or row["integrity_status"] == integrity)
        and (not date_from or str(row["created_at"])[:10] >= date_from)
        and (not date_to or str(row["created_at"])[:10] <= date_to)
    ]


def _evidence_response(
    photo_id: str,
    session: AccountSession,
    repository: Repository,
    evidence: EvidenceStorage,
    *,
    download: bool,
) -> FileResponse:
    record = repository.get_evidence_photo(photo_id)
    if not record or (session.role != "ADMIN" and record["user_id"] != session.user_id):
        raise HTTPException(404, detail={"reason_code": "PHOTO_NOT_FOUND"})
    try:
        path = evidence.resolve(record)
    except FileNotFoundError as error:
        raise HTTPException(404, detail={"reason_code": "PHOTO_NOT_FOUND"}) from error
    if record.get("integrity_status") == "INTEGRITY_FAILED" or not evidence.verify_integrity(photo_id):
        raise HTTPException(409, detail={"reason_code": "EVIDENCE_INTEGRITY_FAILED"})
    suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(str(record["mime_type"]), ".bin")
    date_part = str(record["created_at"])[:10].replace("-", "")
    safe_name = f"biogate_evidence_{date_part}_{photo_id[:12]}{suffix}"
    if download:
        repository.add_audit(
            "EVIDENCE_DOWNLOADED",
            record.get("user_id"),
            "SUCCESS",
            {"photo_id": photo_id, "downloaded_by": session.user_id},
        )
    return FileResponse(
        path,
        media_type=str(record["mime_type"]),
        filename=safe_name,
        content_disposition_type="attachment" if download else "inline",
        headers={"Cache-Control": "private, no-store, max-age=0", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/photos/{photo_id}/download", response_class=FileResponse)
def download_protected_photo(
    photo_id: str,
    session: Annotated[AccountSession, Depends(require_full_account_session)],
    repository: Annotated[Repository, Depends(get_repository)],
    evidence: Annotated[EvidenceStorage, Depends(get_evidence_storage)],
) -> FileResponse:
    return _evidence_response(photo_id, session, repository, evidence, download=True)


@router.get("/photos/{photo_id}", response_class=FileResponse)
def preview_protected_photo(
    photo_id: str,
    session: Annotated[AccountSession, Depends(require_full_account_session)],
    repository: Annotated[Repository, Depends(get_repository)],
    evidence: Annotated[EvidenceStorage, Depends(get_evidence_storage)],
) -> FileResponse:
    return _evidence_response(photo_id, session, repository, evidence, download=False)


@router.post("/admin/archive/run")
def run_archive(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    evidence: Annotated[EvidenceStorage, Depends(get_evidence_storage)],
) -> dict[str, int]:
    return {"archived": evidence.archive_due()}


@router.post("/admin/archive/{photo_id}/verify")
def verify_photo(
    photo_id: str,
    _: Annotated[AccountSession, Depends(require_account_admin)],
    evidence: Annotated[EvidenceStorage, Depends(get_evidence_storage)],
) -> dict[str, bool]:
    try:
        return {"valid": evidence.verify_integrity(photo_id)}
    except FileNotFoundError as error:
        raise HTTPException(404, detail={"reason_code": "PHOTO_NOT_FOUND"}) from error


@router.post("/admin/backups")
def create_backup(
    admin: Annotated[AccountSession, Depends(require_account_admin)],
    backup: Annotated[BackupService, Depends(get_backup_service)],
) -> dict[str, Any]:
    result = backup.create()
    result["created_by"] = admin.user_id
    return result


@router.get("/admin/backups")
def list_backups(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    backup: Annotated[BackupService, Depends(get_backup_service)],
) -> list[dict[str, Any]]:
    return backup.list()


@router.get("/admin/backups/{backup_id}/validate")
def validate_backup(
    backup_id: str,
    _: Annotated[AccountSession, Depends(require_account_admin)],
    backup: Annotated[BackupService, Depends(get_backup_service)],
) -> dict[str, bool]:
    return {"valid": backup.validate(backup_id)}


@router.post("/admin/backups/{backup_id}/restore")
def restore_backup(
    backup_id: str,
    _: BackupRestoreRequest,
    __: Annotated[AccountSession, Depends(require_account_admin)],
    backup: Annotated[BackupService, Depends(get_backup_service)],
) -> dict[str, Any]:
    try:
        return backup.restore(backup_id)
    except ValueError as error:
        raise HTTPException(409, detail={"reason_code": str(error)}) from error


@router.delete("/admin/backups/{backup_id}")
def delete_backup(
    backup_id: str,
    payload: AdminReasonRequest,
    admin: Annotated[AccountSession, Depends(require_account_admin)],
    backup: Annotated[BackupService, Depends(get_backup_service)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, bool]:
    if not backup.delete(backup_id):
        raise HTTPException(404, detail={"reason_code": "BACKUP_NOT_FOUND"})
    repository.add_audit(
        "BACKUP_DELETED", None, "SUCCESS", {"backup_id": backup_id, "reason": payload.reason, "admin_id": admin.user_id}
    )
    return {"deleted": True}
