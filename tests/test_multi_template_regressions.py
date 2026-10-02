import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from app.core.config import Settings
from app.db.database import Database
from app.services.biometric_workflow import BiometricWorkflowError, BiometricWorkflowService
from app.services.biometrics.embedding import deserialize_embedding, serialize_embedding
from app.services.biometrics.models import FaceObservation, FrameAnalysis, QualityResult
from app.services.challenges import ChallengeService, EphemeralCapture
from app.services.face_login import FaceLoginService, FaceLoginState
from app.services.identification import IdentificationService
from app.services.repository import Repository


def _services(tmp_path: Path, name: str = "multi-template.db") -> tuple[Repository, Settings, BiometricWorkflowService]:
    settings = Settings(db_path=tmp_path / name)
    database = Database(settings.db_path)
    database.initialize()
    repository = Repository(database)
    identification = IdentificationService(repository, settings)
    return repository, settings, BiometricWorkflowService(repository, identification)


def _account(repository: Repository, external_id: str, *, status: str = "ACTIVE") -> dict[str, Any]:
    return repository.create_account(
        {"external_id": external_id, "login": external_id, "full_name": external_id, "status": status}
    )


def _vector(*values: float) -> np.ndarray:
    return np.asarray(values, dtype=np.float32)


def _stored_vectors(rows: list[dict[str, Any]]) -> list[np.ndarray]:
    return [deserialize_embedding(row["embedding"], int(row["embedding_dimensions"])) for row in rows]


def _active_vectors(repository: Repository, user_id: int) -> tuple[np.ndarray, list[np.ndarray]]:
    template = repository.get_template(user_id)
    assert template is not None
    centroid = deserialize_embedding(template["embedding"], int(template["embedding_dimensions"]))
    return centroid, _stored_vectors(repository.template_samples(int(template["id"])))


def _analysis(embedding: np.ndarray) -> FrameAnalysis:
    observation = FaceObservation(
        raw=np.zeros(15, dtype=np.float32), confidence=0.99, box=(10, 10, 100, 100), yaw_proxy=0.0
    )
    return FrameAnalysis(embedding, QualityResult(True, 0.95, 100.0, 128.0, "OK"), observation, 0.25, 320, 240)


def _verify_one_to_one(
    repository: Repository,
    settings: Settings,
    identification: IdentificationService,
    external_id: str,
    probe: np.ndarray,
) -> dict[str, Any]:
    service = ChallengeService(repository, settings, identification)
    session = service.create(external_id)
    sequence = json.loads(session["expected_sequence"])
    capture = EphemeralCapture(
        embeddings=[probe for _ in sequence], quality_scores=[0.95 for _ in sequence]
    )
    return service._finalize(session, sequence, capture, _analysis(probe))


def _recover(
    repository: Repository,
    settings: Settings,
    identification: IdentificationService,
    user_id: int | None,
    probe: np.ndarray,
) -> dict[str, Any]:
    challenge = ChallengeService(repository, settings, identification)
    service = FaceLoginService(repository, settings, challenge, identification)
    state = FaceLoginState(
        id="recovery-state",
        mode="RECOVERY",
        sequence=["CENTER"],
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
        target_user_id=user_id,
    )
    return service._targeted_recovery(state, probe)


@pytest.mark.parametrize(
    ("trigger_name", "trigger_sql"),
    [
        (
            "fail_after_centroid_write",
            """CREATE TRIGGER fail_after_centroid_write AFTER UPDATE ON biometric_templates
               BEGIN SELECT RAISE(ABORT, 'injected after centroid write'); END""",
        ),
        (
            "fail_after_old_representative_delete",
            """CREATE TRIGGER fail_after_old_representative_delete AFTER DELETE ON biometric_template_samples
               BEGIN SELECT RAISE(ABORT, 'injected after representative delete'); END""",
        ),
        (
            "fail_before_request_approved",
            """CREATE TRIGGER fail_before_request_approved BEFORE UPDATE OF status ON biometric_requests
               WHEN NEW.status='APPROVED'
               BEGIN SELECT RAISE(ABORT, 'injected before request approved'); END""",
        ),
    ],
)
def test_approval_faults_roll_back_centroid_representatives_and_request(
    tmp_path: Path, trigger_name: str, trigger_sql: str
) -> None:
    repository, settings, workflow = _services(tmp_path, f"{trigger_name}.db")
    user = _account(repository, trigger_name)
    user_id = int(user["id"])
    old_centroid = _vector(1.0, 0.0, 0.0)
    old_representatives = [_vector(1.0, 0.0, 0.0), _vector(0.8, 0.6, 0.0)]
    repository.upsert_template(
        trigger_name,
        "test",
        "1",
        serialize_embedding(old_centroid),
        3,
        len(old_representatives),
        [(serialize_embedding(item), 3) for item in old_representatives],
    )
    request = workflow.submit(
        user_id,
        _vector(0.0, 1.0, 0.0),
        "test",
        "2",
        2,
        [_vector(0.0, 1.0, 0.0), _vector(0.0, 0.8, 0.6)],
    )
    with repository.db.write() as connection:
        connection.execute(trigger_sql)

    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        workflow.review(str(request["id"]), "APPROVED", None)

    centroid, representatives = _active_vectors(repository, user_id)
    assert np.allclose(centroid, old_centroid)
    assert len(representatives) == len(old_representatives)
    assert all(
        np.allclose(actual, expected)
        for actual, expected in zip(representatives, old_representatives, strict=True)
    )
    assert repository.get_biometric_request(str(request["id"]))["status"] == "PENDING_REVIEW"  # type: ignore[index]
    with repository.db.write() as connection:
        connection.execute(f"DROP TRIGGER {trigger_name}")


@pytest.mark.parametrize("competing_decision", ["APPROVED", "REJECTED", "REVISION_REQUIRED"])
def test_approval_state_conflicts_have_exactly_one_winner(tmp_path: Path, competing_decision: str) -> None:
    repository, _, workflow = _services(tmp_path, f"race-{competing_decision}.db")
    user = _account(repository, f"race-{competing_decision}")
    user_id = int(user["id"])
    old = _vector(1.0, 0.0, 0.0)
    new = _vector(0.0, 1.0, 0.0)
    repository.upsert_template(str(user["external_id"]), "test", "1", serialize_embedding(old), 3, 1)
    request = workflow.submit(user_id, new, "test", "2", 1, [new])

    def review(decision: str) -> str:
        try:
            workflow.review(
                str(request["id"]), decision, None, "Retake" if decision == "REVISION_REQUIRED" else ""
            )
            return decision
        except BiometricWorkflowError as error:
            return error.reason_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(review, ["APPROVED", competing_decision]))

    assert outcomes.count("BIOMETRIC_REQUEST_NOT_REVIEWABLE") == 1
    final = repository.get_biometric_request(str(request["id"]))
    assert final is not None and final["status"] in {"APPROVED", competing_decision}
    centroid, _ = _active_vectors(repository, user_id)
    assert np.allclose(centroid, new if final["status"] == "APPROVED" else old)


def test_repeated_approve_of_finalized_request_is_controlled_conflict(tmp_path: Path) -> None:
    repository, _, workflow = _services(tmp_path)
    user = _account(repository, "approve-twice")
    probe = _vector(0.0, 1.0, 0.0)
    request = workflow.submit(int(user["id"]), probe, "test", "1", 1, [probe])
    workflow.review(str(request["id"]), "APPROVED", None)

    with pytest.raises(BiometricWorkflowError, match="BIOMETRIC_REQUEST_NOT_REVIEWABLE"):
        workflow.review(str(request["id"]), "APPROVED", None)

    assert repository.get_biometric_request(str(request["id"]))["status"] == "APPROVED"  # type: ignore[index]


def test_duplicate_submit_has_one_pending_request_and_no_orphan_samples(tmp_path: Path) -> None:
    repository, _, workflow = _services(tmp_path)
    user = _account(repository, "duplicate-submit")
    probe = _vector(1.0, 0.0, 0.0)

    def submit() -> str:
        try:
            request = workflow.submit(
                int(user["id"]), probe, "test", "1", 2, [probe, _vector(0.8, 0.6, 0.0)]
            )
            return str(request["status"])
        except BiometricWorkflowError as error:
            return error.reason_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: submit(), range(2)))

    assert sorted(outcomes) == ["ACTIVE_BIOMETRIC_REQUEST_EXISTS", "PENDING_REVIEW"]
    with repository.db.connect() as connection:
        pending = connection.execute(
            "SELECT COUNT(*) FROM biometric_requests WHERE user_id=? AND status='PENDING_REVIEW'", (user["id"],)
        ).fetchone()[0]
        orphaned = connection.execute(
            """SELECT COUNT(*) FROM biometric_request_samples sample
               LEFT JOIN biometric_requests request ON request.id=sample.request_id WHERE request.id IS NULL"""
        ).fetchone()[0]
    assert pending == 1
    assert orphaned == 0


def test_revision_approval_activates_only_latest_revision_samples(tmp_path: Path) -> None:
    repository, _, workflow = _services(tmp_path)
    user = _account(repository, "revision-isolation")
    samples_a = [_vector(1.0, 0.0, 0.0), _vector(0.8, 0.6, 0.0)]
    samples_b = [_vector(0.0, 1.0, 0.0), _vector(0.0, 0.8, 0.6)]
    first = workflow.submit(int(user["id"]), samples_a[0], "test", "1", 2, samples_a)
    workflow.review(str(first["id"]), "REVISION_REQUIRED", None, "Retake")
    second = workflow.submit(int(user["id"]), samples_b[0], "test", "1", 2, samples_b)
    workflow.review(str(second["id"]), "APPROVED", None)

    _, active = _active_vectors(repository, int(user["id"]))
    assert len(active) == len(samples_b)
    assert all(np.allclose(actual, expected) for actual, expected in zip(active, samples_b, strict=True))
    assert all(not np.allclose(actual, samples_a[0]) for actual in active)
    assert repository.get_biometric_request(str(first["id"]))["status"] == "CANCELLED"  # type: ignore[index]


def test_pending_samples_are_isolated_until_approval_across_all_scoring_paths(tmp_path: Path) -> None:
    repository, settings, workflow = _services(tmp_path)
    identification = IdentificationService(repository, settings)
    user = _account(repository, "pending-isolation")
    user_id = int(user["id"])
    centroid = _vector(1.0, 0.0, 0.0)
    probe = _vector(0.0, 1.0, 0.0)
    repository.upsert_template("pending-isolation", "test", "1", serialize_embedding(centroid), 3, 1)
    request = workflow.submit(user_id, centroid, "test", "2", 1, [probe])

    assert identification.identify(probe).decision == "UNKNOWN"
    assert identification.possible_duplicate(probe, excluding_user_id=999) is None
    assert _verify_one_to_one(repository, settings, identification, "pending-isolation", probe)["result"] == "REJECTED"
    assert _recover(repository, settings, identification, user_id, probe)["decision"] == "DENIED"

    workflow.review(str(request["id"]), "APPROVED", None)

    assert identification.identify(probe).decision == "IDENTIFIED"
    assert identification.possible_duplicate(probe, excluding_user_id=999) is not None
    assert _verify_one_to_one(repository, settings, identification, "pending-isolation", probe)["result"] == "VERIFIED"
    assert _recover(repository, settings, identification, user_id, probe)["decision"] == "RECOVERY_VERIFIED"


def test_one_to_one_succeeds_from_representative_when_centroid_is_below_threshold(tmp_path: Path) -> None:
    repository, settings, _ = _services(tmp_path)
    identification = IdentificationService(repository, settings)
    _account(repository, "representative-verify")
    centroid = _vector(1.0, 0.0, 0.0)
    representative = _vector(0.0, 1.0, 0.0)
    repository.upsert_template(
        "representative-verify", "test", "1", serialize_embedding(centroid), 3, 1,
        [(serialize_embedding(representative), 3)],
    )

    assert float(np.dot(centroid, representative)) < 0.363
    result = _verify_one_to_one(repository, settings, identification, "representative-verify", representative)
    assert result["similarity_score"] > 0.363
    assert result["result"] == "VERIFIED"


def test_legacy_pending_request_uses_centroid_as_single_representative_on_approve(tmp_path: Path) -> None:
    repository, _, workflow = _services(tmp_path)
    user = _account(repository, "legacy-pending")
    centroid = _vector(0.2, 0.9, 0.1)
    request = repository.create_biometric_request(
        {
            "id": "legacy-request",
            "user_id": user["id"],
            "candidate_embedding": serialize_embedding(centroid),
            "embedding_dimensions": 3,
            "model_name": "legacy",
            "model_version": "1",
            "sample_count": 1,
        }
    )
    assert repository.biometric_request_samples(str(request["id"])) == []

    workflow.review(str(request["id"]), "APPROVED", None)

    active_centroid, active_samples = _active_vectors(repository, int(user["id"]))
    expected = deserialize_embedding(serialize_embedding(centroid), 3)
    assert np.allclose(active_centroid, expected)
    assert len(active_samples) == 1
    assert np.allclose(active_samples[0], expected)


def test_identity_scoring_ignores_representatives_beyond_active_bound(tmp_path: Path) -> None:
    repository, settings, _ = _services(tmp_path)
    user = _account(repository, "bounded-score")
    centroid = _vector(1.0, 0.0, 0.0)
    nonmatches = [_vector(1.0, 0.0, 0.0) for _ in range(5)]
    hidden_match = _vector(0.0, 1.0, 0.0)
    repository.upsert_template(
        "bounded-score",
        "test",
        "1",
        serialize_embedding(centroid),
        3,
        6,
        [(serialize_embedding(item), 3) for item in [*nonmatches, hidden_match]],
    )
    template = repository.get_template(int(user["id"]))
    assert template is not None

    score = IdentificationService(repository, settings).identity_score(hidden_match, template)

    assert score < settings.identification_threshold
