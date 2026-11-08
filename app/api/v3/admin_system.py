"""Dashboard, system health, runtime settings and the live monitor."""

import asyncio
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.api.dependencies import (
    get_app_settings,
    get_pipeline,
    get_repository,
    require_account_admin,
)
from app.api.v3._common import (
    V3_RUNTIME_SETTINGS,
)
from app.core.config import Settings
from app.schemas.v3 import (
    AdminSettingsRequest,
)
from app.services.account_auth import AccountSession
from app.services.biometrics.pipeline import BiometricPipeline
from app.services.maintenance import disk_status
from app.services.repository import Repository

router = APIRouter()


@router.get("/admin/dashboard")
def v3_dashboard(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, Any]:
    return {"metrics": repository.v3_admin_stats(), "recent_access": repository.access_events(limit=20)}


@router.get("/admin/system-health")
def system_health(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    pipeline: Annotated[BiometricPipeline, Depends(get_pipeline)],
) -> dict[str, Any]:
    with repository.db.connect() as conn:
        database_ok = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        storage = conn.execute(
            """SELECT storage_state,COUNT(*) AS files,COALESCE(SUM(size_bytes),0) AS size_bytes,
               MIN(created_at) AS oldest,MAX(created_at) AS newest
               FROM evidence_photos GROUP BY storage_state"""
        ).fetchall()
        last_backup = conn.execute(
            "SELECT created_at,validation_status FROM backups WHERE kind='USER' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    storage_rows = {row["storage_state"]: dict(row) for row in storage}
    disk = disk_status(settings)
    return {
        "components": {
            "database": "OK" if database_ok else "ERROR",
            "yunet": "READY" if getattr(pipeline, "detector", None) else "ERROR",
            "sface": "READY" if getattr(pipeline, "embedder", None) else "ERROR",
            "mediapipe": "READY" if getattr(pipeline, "eye_geometry", None) else "ERROR",
            "active_storage": "OK" if settings.active_image_dir.parent.exists() else "WARNING",
            "archive": "OK" if settings.archive_dir.parent.exists() else "WARNING",
            "disk": "WARNING" if disk["low"] else "OK",
        },
        "disk": disk,
        "storage": {
            "active": storage_rows.get("ACTIVE", {"files": 0, "size_bytes": 0, "oldest": None, "newest": None}),
            "archive": storage_rows.get("ARCHIVED", {"files": 0, "size_bytes": 0, "oldest": None, "newest": None}),
        },
        "last_backup": dict(last_backup) if last_backup else None,
    }


@router.get("/admin/settings")
def v3_settings(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict[str, Any]:
    result = {name: getattr(settings, name) for name in V3_RUNTIME_SETTINGS}
    result.update(
        {"require_liveness": True, "active_image_days": settings.active_image_days, "permanent_archive": True}
    )
    return result


@router.put("/admin/settings")
def update_v3_settings(
    payload: AdminSettingsRequest,
    admin: Annotated[AccountSession, Depends(require_account_admin)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, Any]:
    changes = payload.model_dump(exclude_none=True)
    target_face = bool(changes.get("allow_face_login", settings.allow_face_login))
    target_password = bool(changes.get("allow_password_login", settings.allow_password_login))
    if not target_face and not target_password:
        raise HTTPException(409, detail={"reason_code": "GLOBAL_AUTH_METHOD_REQUIRED"})
    repository.set_runtime_settings({f"v3_{name}": json.dumps(value) for name, value in changes.items()}, admin.user_id)
    for name, value in changes.items():
        setattr(settings, name, value)
    return v3_settings(admin, settings)


@router.get("/admin/live")
async def live_monitor(
    request: Request,
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> StreamingResponse:
    async def stream() -> Any:
        last_id = 0
        while not await request.is_disconnected():
            # SQLite is synchronous; query it in the thread pool so the event loop stays free.
            events = await run_in_threadpool(repository.access_events, limit=50, after_id=last_id)
            for event in reversed(events):
                last_id = max(last_id, int(event["id"]))
                yield f"event: access\ndata: {json.dumps(event, default=str)}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(stream(), media_type="text/event-stream")
