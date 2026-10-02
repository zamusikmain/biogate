import numpy as np
import pytest

from app.services.biometrics.embedding import (
    cosine_similarity,
    deserialize_embedding,
    serialize_embedding,
)
from app.services.biometrics.models import FaceObservation, QualityResult
from app.services.biometrics.pipeline import BiometricPipeline
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


def test_liveness_pipeline_skips_eyes_and_embedding_for_pose_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = object.__new__(BiometricPipeline)
    image = np.zeros((120, 160, 3), dtype=np.uint8)
    observation = FaceObservation(np.zeros(15, dtype=np.float32), 0.99, (10, 10, 100, 100), 0.0)
    quality = QualityResult(True, 0.9, 100.0, 128.0, "OK")
    eye_calls = 0

    monkeypatch.setattr(pipeline, "decode", staticmethod(lambda _: image))
    pipeline.detector = type("Detector", (), {"detect_one": lambda self, _: observation})()
    pipeline.quality = type("Quality", (), {"evaluate": lambda self, _, __: quality})()
    pipeline.embedder = type("Embedder", (), {"extract": lambda self, _, __: (_ for _ in ()).throw(AssertionError())})()

    def eye_ratios(_: object) -> tuple[float, float]:
        nonlocal eye_calls
        eye_calls += 1
        return (0.25, 0.25)

    pipeline.eye_geometry = type("Eyes", (), {"eye_aspect_ratios": staticmethod(eye_ratios)})()
    result = pipeline.analyze_liveness_frame(b"pose", analyze_eyes=False)
    assert result.embedding.size == 0
    assert result.eye_aspect_ratio is None
    assert eye_calls == 0
    pipeline.analyze_liveness_frame(b"blink", analyze_eyes=True)
    assert eye_calls == 1
