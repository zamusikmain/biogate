import numpy as np
import pytest

from app.services.biometrics.embedding import (
    cosine_similarity,
    deserialize_embedding,
    serialize_embedding,
)
from evaluation.metrics import evaluate_threshold


def test_similarity_and_threshold_decision() -> None:
    base = np.asarray([1.0, 0.0], dtype=np.float32)
    assert cosine_similarity(base, base) == pytest.approx(1.0)
    assert cosine_similarity(base, np.asarray([0.0, 1.0], dtype=np.float32)) == pytest.approx(0.0)
    scores = np.asarray([0.8, 0.7, 0.2, 0.1])
    labels = np.asarray([True, True, False, False])
    result = evaluate_threshold(scores, labels, 0.5)
    assert result.far == 0 and result.frr == 0


def test_embedding_round_trip_and_validation() -> None:
    embedding = np.asarray([3.0, 4.0], dtype=np.float32)
    restored = deserialize_embedding(serialize_embedding(embedding), 2)
    assert restored.tolist() == pytest.approx([0.6, 0.8])
    with pytest.raises(ValueError):
        deserialize_embedding(b"short", 2)
