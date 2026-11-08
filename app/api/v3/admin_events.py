"""Access, security and audit event views for administrators."""

import csv
import io
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api.dependencies import (
    get_repository,
    require_account_admin,
)
from app.api.event_safety import safe_event_row
from app.api.v3._common import (
    _account_display_identity,
)
from app.schemas.v3 import (
    SecurityReviewRequest,
)
from app.services.account_auth import AccountSession
from app.services.repository import Repository
from app.services.risk import annotate_risk

router = APIRouter()


@router.get("/admin/access-events")
def all_access_events(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: str | None = None,
    method: str | None = Query(default=None, pattern=r"^(FACE|PASSWORD)$"),
    result: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    return repository.access_events(
        limit=limit,
        offset=offset,
        search=user,
        method=method,
        result=result,
        date_from=date_from,
        date_to=date_to,
    )


@router.get("/admin/access-events.csv")
def export_access_events(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
    user: str | None = None,
    method: str | None = Query(default=None, pattern=r"^(FACE|PASSWORD)$"),
    result: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> Response:
    output = io.StringIO()
    columns = [
        "timestamp",
        "user_display_name",
        "login",
        "role",
        "method",
        "result",
        "reason",
        "similarity",
        "liveness",
        "client_address",
        "user_agent",
    ]
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    rows = repository.access_events(
        limit=10000,
        search=user,
        method=method,
        result=result,
        date_from=date_from,
        date_to=date_to,
    )
    writer.writerows(
        {
            **row,
            "user_display_name": _account_display_identity(row),
            "login": row.get("login") or "",
            "role": row.get("role") or "",
            "liveness": row.get("liveness_score"),
        }
        for row in rows
    )
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=access-events.csv"},
    )


@router.get("/admin/security-events")
def all_security_events(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    rows = repository.security_events(limit, offset)
    context = repository.security_events(1000, 0)
    return [safe_event_row(row) for row in annotate_risk(rows, context)]


@router.get("/admin/audit-events")
def all_audit_events(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    return [safe_event_row(row) for row in repository.audit_events(limit, offset)]


@router.get("/admin/unknown-faces")
def unknown_faces(
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    return [safe_event_row(row) for row in repository.unknown_faces(limit, offset)]


@router.get("/admin/events/{category}/{event_id}")
def admin_event_details(
    category: str,
    event_id: int,
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, Any]:
    queries = {
        "access": """SELECT ae.*,u.external_id,u.login,u.full_name,u.role,p.id AS photo_id FROM access_events ae
                     LEFT JOIN users u ON u.id=ae.user_id LEFT JOIN evidence_photos p ON p.access_event_id=ae.id
                     WHERE ae.id=?""",
        "unknown": """SELECT ae.*,p.id AS photo_id,se.severity,se.metadata,se.reviewed_at,se.note
                      FROM access_events ae LEFT JOIN evidence_photos p ON p.access_event_id=ae.id
                      LEFT JOIN security_events se ON se.access_event_id=ae.id WHERE ae.id=?""",
        "security": """SELECT se.*,u.full_name,u.login,u.role,u.external_id FROM security_events se
                       LEFT JOIN users u ON u.id=se.user_id WHERE se.id=?""",
        "audit": """SELECT ae.*,u.full_name,u.login,u.role,u.external_id FROM audit_events ae
                    LEFT JOIN users u ON u.id=ae.user_id WHERE ae.id=?""",
    }
    query = queries.get(category)
    if not query:
        raise HTTPException(404, detail={"reason_code": "EVENT_NOT_FOUND"})
    with repository.db.connect() as conn:
        row = conn.execute(query, (event_id,)).fetchone()
    if not row:
        raise HTTPException(404, detail={"reason_code": "EVENT_NOT_FOUND"})
    result = dict(row)
    if category == "security":
        result = annotate_risk([result], repository.security_events(1000, 0))[0]
    action = repository.password_reset_action_metadata(result.get("metadata"), result.get("user_id"))
    if action:
        result["action_metadata"] = action
    return safe_event_row(result)


@router.post("/admin/security-events/{event_id}/review")
def review_security_event(
    event_id: int,
    payload: SecurityReviewRequest,
    _: Annotated[AccountSession, Depends(require_account_admin)],
    repository: Annotated[Repository, Depends(get_repository)],
) -> dict[str, bool]:
    if not repository.review_security_event(event_id, payload.note):
        raise HTTPException(404, detail={"reason_code": "SECURITY_EVENT_NOT_FOUND"})
    return {"reviewed": True}
