"""Housekeeping that used to need an administrator's button press.

Runs once at startup and then every ``maintenance_interval_seconds``: moves evidence
photos older than ``active_image_days`` to the archive, deletes legacy attempt
snapshots past their retention date and warns when the data disk runs low.
"""

import asyncio
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.services.repository import Repository
from app.services.storage import EvidenceStorage

logger = logging.getLogger("biogate.maintenance")


def cleanup_expired_attempt_snapshots(repository: Repository, settings: Settings) -> int:
    """Delete legacy V2 attempt snapshots past their retention date."""
    deleted = 0
    root = settings.attempt_image_dir.resolve()
    for snapshot in repository.expired_snapshots(datetime.now(UTC).isoformat()):
        path = (root / str(snapshot["file_name"])).resolve()
        if path.parent == root:
            path.unlink(missing_ok=True)
        repository.delete_snapshot(int(snapshot["attempt_id"]))
        deleted += 1
    return deleted


def _existing_parent(path: Path) -> Path:
    candidate = path.resolve()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def disk_status(settings: Settings) -> dict[str, Any]:
    """Free space on the volume that holds the database and evidence photos."""
    usage = shutil.disk_usage(_existing_parent(settings.db_path.parent))
    threshold = settings.min_free_disk_mb * 1024 * 1024
    return {
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "threshold_bytes": threshold,
        "low": usage.free < threshold,
    }


def run_maintenance(repository: Repository, evidence: EvidenceStorage, settings: Settings) -> dict[str, Any]:
    result: dict[str, Any] = {"archived": 0, "snapshots_deleted": 0, "disk_low": False}
    try:
        result["archived"] = evidence.archive_due()
    except OSError:
        logger.exception("Evidence archiving failed", extra={"operation": "maintenance"})
    try:
        result["snapshots_deleted"] = cleanup_expired_attempt_snapshots(repository, settings)
    except OSError:
        logger.exception("Snapshot cleanup failed", extra={"operation": "maintenance"})
    disk = disk_status(settings)
    result["disk_low"] = disk["low"]
    if disk["low"]:
        logger.warning(
            "Low free disk space for BioGate data",
            extra={"operation": "maintenance", "reason_code": "LOW_DISK_SPACE"},
        )
        repository.add_security_event(
            "LOW_DISK_SPACE",
            "WARNING",
            None,
            {"free_mb": disk["free_bytes"] // (1024 * 1024), "threshold_mb": settings.min_free_disk_mb},
        )
    logger.info("Maintenance completed", extra={"operation": "maintenance", "decision": "SUCCESS"})
    return result


async def maintenance_loop(repository: Repository, evidence: EvidenceStorage, settings: Settings) -> None:
    while True:
        await asyncio.sleep(settings.maintenance_interval_seconds)
        try:
            await asyncio.to_thread(run_maintenance, repository, evidence, settings)
        except Exception:  # the loop must survive one bad run
            logger.exception("Maintenance run failed", extra={"operation": "maintenance"})
