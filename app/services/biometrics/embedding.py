from pathlib import Path

import cv2
import numpy as np
from numpy.typing import NDArray

from app.services.biometrics.models import FaceObservation


class EmbeddingService:
    model_name = "OpenCV-SFace"
    model_version = "2021dec"

    def __init__(self, model_path: Path):
        self.recognizer = cv2.FaceRecognizerSF.create(
            str(model_path), "", cv2.dnn.DNN_BACKEND_OPENCV, cv2.dnn.DNN_TARGET_CPU
        )

    def extract(self, image: NDArray[np.uint8], face: FaceObservation) -> NDArray[np.float32]:
        aligned = self.recognizer.alignCrop(image, face.raw)
        feature = np.asarray(self.recognizer.feature(aligned), dtype=np.float32).reshape(-1)
        return normalize(feature)


def normalize(embedding: NDArray[np.float32]) -> NDArray[np.float32]:
    norm = float(np.linalg.norm(embedding))
    if not np.isfinite(norm) or norm <= 1e-12:
        raise ValueError("Invalid zero or non-finite embedding")
    return np.asarray(embedding / norm, dtype=np.float32)


def aggregate(embeddings: list[NDArray[np.float32]]) -> NDArray[np.float32]:
    if not embeddings:
        raise ValueError("At least one embedding is required")
    return normalize(np.mean(np.stack(embeddings), axis=0).astype(np.float32))


def select_representatives(
    embeddings: list[NDArray[np.float32]], *, maximum: int = 5, duplicate_similarity: float = 0.995
) -> list[NDArray[np.float32]]:
    """Select a bounded, deterministic diverse subset without retaining near duplicates."""
    if maximum < 1:
        raise ValueError("maximum must be positive")
    unique: list[NDArray[np.float32]] = []
    for embedding in embeddings:
        normalized = normalize(embedding)
        if not any(cosine_similarity(normalized, existing) >= duplicate_similarity for existing in unique):
            unique.append(normalized)
    if not unique:
        raise ValueError("At least one embedding is required")
    centroid = aggregate(unique)
    first = max(range(len(unique)), key=lambda index: (cosine_similarity(unique[index], centroid), -index))
    selected = [unique.pop(first)]
    while unique and len(selected) < maximum:
        index = max(
            range(len(unique)),
            key=lambda candidate: (
                min(1.0 - cosine_similarity(unique[candidate], item) for item in selected),
                -candidate,
            ),
        )
        selected.append(unique.pop(index))
    return selected


def cosine_similarity(left: NDArray[np.float32], right: NDArray[np.float32]) -> float:
    if left.shape != right.shape:
        raise ValueError("Embedding dimensions do not match")
    return float(np.clip(np.dot(normalize(left), normalize(right)), -1.0, 1.0))


def serialize_embedding(embedding: NDArray[np.float32]) -> bytes:
    normalized = normalize(embedding)
    return normalized.astype("<f4", copy=False).tobytes(order="C")


def deserialize_embedding(data: bytes, dimensions: int) -> NDArray[np.float32]:
    if dimensions <= 0 or len(data) != dimensions * 4:
        raise ValueError("Invalid embedding payload length")
    result = np.frombuffer(data, dtype="<f4").copy()
    if not np.all(np.isfinite(result)):
        raise ValueError("Embedding contains non-finite values")
    return result
