import json
import sqlite3
import uuid
from typing import Any

import numpy as np
from numpy.typing import NDArray

from app.services.biometrics.embedding import select_representatives, serialize_embedding
from app.services.identification import IdentificationService
from app.services.repository import Repository, utc_now

ACTIVE_REQUEST_STATUSES = {"DRAFT", "PENDING_REVIEW", "REVISION_REQUIRED"}


class BiometricWorkflowError(Exception):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class BiometricWorkflowService:
    def __init__(self, repository: Repository, identification: IdentificationService):
        self.repository = repository
        self.identification = identification

    def submit(
        self,
        user_id: int,
        template: NDArray[np.float32],
        model_name: str,
        model_version: str,
        sample_count: int,
        representatives: list[NDArray[np.float32]] | None = None,
    ) -> dict[str, Any]:
        active = self.repository.active_biometric_request(user_id)
        if active and active["status"] in {"DRAFT", "PENDING_REVIEW"}:
            raise BiometricWorkflowError("ACTIVE_BIOMETRIC_REQUEST_EXISTS")
        if active and active["status"] == "REVISION_REQUIRED":
            self.repository.update_biometric_request(str(active["id"]), "CANCELLED", "Superseded by revision")
        duplicate = self.identification.possible_duplicate(template, excluding_user_id=user_id)
        request_id = str(uuid.uuid4())
        try:
            selected = select_representatives(representatives or [template])
            request = self.repository.create_biometric_request(
                {
                    "id": request_id,
                    "user_id": user_id,
                    "status": "PENDING_REVIEW",
                    "candidate_embedding": serialize_embedding(template),
                    "embedding_dimensions": int(template.size),
                    "model_name": model_name,
                    "model_version": model_version,
                    "sample_count": sample_count,
                    "duplicate_user_id": duplicate.user_id if duplicate else None,
                    "duplicate_similarity": duplicate.similarity if duplicate else None,
                },
                [(serialize_embedding(sample), int(sample.size)) for sample in selected],
            )
        except sqlite3.IntegrityError as error:
            raise BiometricWorkflowError("ACTIVE_BIOMETRIC_REQUEST_EXISTS") from error
        self.repository.add_notification(None, "ADMIN", "BIOMETRIC_REQUEST", "BIOMETRIC_UPDATE_REQUESTED")
        self.repository.add_notification(user_id, None, "BIOMETRIC_REQUEST", "BIOMETRIC_SUBMITTED")
        self.repository.add_security_event("BIOMETRIC_UPDATE_REQUESTED", "INFO", user_id, {"request_id": request_id})
        if duplicate:
            self.repository.add_security_event(
                "POSSIBLE_DUPLICATE",
                "CRITICAL",
                user_id,
                {
                    "request_id": request_id,
                    "existing_user_id": duplicate.user_id,
                    "similarity": duplicate.similarity,
                },
            )
        return self._public(request)

    def review(self, request_id: str, decision: str, admin_user_id: int | None, comment: str = "") -> None:
        request = self.repository.get_biometric_request(request_id)
        if not request or request["status"] != "PENDING_REVIEW":
            raise BiometricWorkflowError("BIOMETRIC_REQUEST_NOT_REVIEWABLE")
        if decision == "REVISION_REQUIRED" and not comment.strip():
            raise BiometricWorkflowError("ADMIN_COMMENT_REQUIRED")
        if decision not in {"APPROVED", "REJECTED", "REVISION_REQUIRED"}:
            raise BiometricWorkflowError("INVALID_REVIEW_DECISION")
        if decision == "APPROVED" and request["duplicate_user_id"] is not None:
            raise BiometricWorkflowError("POSSIBLE_DUPLICATE_REQUIRES_RESOLUTION")
        if decision == "APPROVED":
            user = self.repository.get_user_by_id(int(request["user_id"]))
            if not user:
                raise BiometricWorkflowError("USER_NOT_FOUND")
            try:
                self.repository.approve_biometric_request(request, admin_user_id, comment)
            except ValueError as error:
                raise BiometricWorkflowError(str(error)) from error
        else:
            if not self.repository.update_biometric_request(
                request_id, decision, comment, admin_user_id, expected_status="PENDING_REVIEW"
            ):
                raise BiometricWorkflowError("BIOMETRIC_REQUEST_NOT_REVIEWABLE")
        event = {
            "APPROVED": "BIOMETRIC_APPROVED",
            "REJECTED": "BIOMETRIC_REJECTED",
            "REVISION_REQUIRED": "BIOMETRIC_REVISION_REQUIRED",
        }[decision]
        if decision != "APPROVED":
            with self.repository.db.write() as conn:
                conn.execute(
                    """INSERT INTO biometric_history(user_id,request_id,event_type,timestamp,metadata)
                       VALUES(?,?,?,?,?)""",
                    (request["user_id"], request_id, event, utc_now(), json.dumps({"comment": comment})),
                )
            self.repository.add_audit(event, int(request["user_id"]), "SUCCESS", {"request_id": request_id})
        self.repository.add_notification(int(request["user_id"]), None, event, event)

    def cancel(self, request_id: str, user_id: int) -> None:
        request = self.repository.get_biometric_request(request_id)
        if not request or int(request["user_id"]) != user_id or request["status"] != "PENDING_REVIEW":
            raise BiometricWorkflowError("BIOMETRIC_REQUEST_NOT_CANCELLABLE")
        self.repository.update_biometric_request(request_id, "CANCELLED")
        self.repository.add_audit("BIOMETRIC_REQUEST_CANCELLED", user_id, "SUCCESS", {"request_id": request_id})

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in record.items() if key != "candidate_embedding"}
