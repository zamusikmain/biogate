from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from app.core.config import Settings
from app.services.biometrics.embedding import cosine_similarity, deserialize_embedding
from app.services.repository import Repository


@dataclass(frozen=True)
class IdentificationCandidate:
    user_id: int
    external_id: str
    login: str
    full_name: str
    status: str
    role: str
    similarity: float


@dataclass(frozen=True)
class IdentificationResult:
    decision: str
    best: IdentificationCandidate | None
    second: IdentificationCandidate | None
    threshold: float
    margin: float | None


class IdentificationService:
    """Threshold and margin-gated 1:N search over locally stored templates."""

    def __init__(self, repository: Repository, settings: Settings):
        self.repository = repository
        self.settings = settings

    def identify(self, probe: NDArray[np.float32]) -> IdentificationResult:
        candidates: list[IdentificationCandidate] = []
        for record in self.repository.identification_templates():
            similarity = self.identity_score(probe, record)
            candidates.append(
                IdentificationCandidate(
                    user_id=int(record["user_id"]),
                    external_id=str(record["external_id"]),
                    login=str(record["login"]),
                    full_name=str(record["full_name"]),
                    status=str(record["status"]),
                    role=str(record["role"]),
                    similarity=similarity,
                )
            )
        candidates.sort(key=lambda item: item.similarity, reverse=True)
        best = candidates[0] if candidates else None
        second = candidates[1] if len(candidates) > 1 else None
        margin = best.similarity - second.similarity if best and second else None
        if best is None or best.similarity < self.settings.identification_threshold:
            decision = "UNKNOWN"
        elif margin is not None and margin < self.settings.identification_ambiguity_margin:
            decision = "AMBIGUOUS"
        elif best.status == "BLOCKED":
            decision = "BLOCKED"
        elif best.status == "DISABLED":
            decision = "DISABLED"
        else:
            decision = "IDENTIFIED"
        return IdentificationResult(decision, best, second, self.settings.identification_threshold, margin)

    def identity_score(self, probe: NDArray[np.float32], record: dict[str, Any]) -> float:
        """Maximum centroid/representative cosine score over a bounded active set."""
        centroid = deserialize_embedding(record["embedding"], int(record["embedding_dimensions"]))
        scores = [cosine_similarity(probe, centroid)]
        for sample in self.repository.template_samples(int(record["id"])):
            sample_embedding = deserialize_embedding(sample["embedding"], int(sample["embedding_dimensions"]))
            scores.append(cosine_similarity(probe, sample_embedding))
        return max(scores)

    def possible_duplicate(
        self, candidate: NDArray[np.float32], *, excluding_user_id: int
    ) -> IdentificationCandidate | None:
        matches: list[IdentificationCandidate] = []
        for record in self.repository.active_templates(excluding_user_id=excluding_user_id):
            similarity = self.identity_score(candidate, record)
            if similarity >= self.settings.duplicate_biometric_threshold:
                matches.append(
                    IdentificationCandidate(
                        user_id=int(record["user_id"]),
                        external_id=str(record["external_id"]),
                        login=str(record["login"]),
                        full_name=str(record["full_name"]),
                        status=str(record["status"]),
                        role=str(record["role"]),
                        similarity=similarity,
                    )
                )
        if not matches:
            return None
        return max(matches, key=lambda item: item.similarity)
