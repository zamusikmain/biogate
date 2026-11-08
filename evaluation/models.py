"""Face acquisition and embedding back-ends compared by the evaluation tool.

Acquisition always uses the production YuNet detector and the production 5-point alignment
(``FaceRecognizerSF.alignCrop``, 112x112 ArcFace template). Only the embedding network differs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np
from numpy.typing import NDArray

from app.services.biometrics.detection import FaceDetector
from app.services.biometrics.embedding import normalize
from app.services.biometrics.model_store import SFACE, YUNET, ModelSpec, ModelStore
from app.services.biometrics.models import FaceObservation
from app.services.biometrics.quality import QualityService

# Separate model, used only for local comparison: weights are never committed (see docs/EVALUATION.md).
ARCFACE_R100 = ModelSpec(
    "arcfaceresnet100-8.onnx",
    "https://github.com/onnx/models/raw/4c46cd00fbdb7cd30b6c1c17ab54f2e1f4f7b177/"
    "validated/vision/body_analysis/arcface/model/arcfaceresnet100-8.onnx",
    "f3a6bc281e72f88862f5748b53be3d76b3b48f8f1ab1f4a537941bdc4e1b01da",
    max_bytes=300 * 1024 * 1024,
)


@dataclass(frozen=True)
class Acquisition:
    """Outcome of finding the evaluated face in one image."""

    status: str  # OK, NO_FACE or DECODE_ERROR
    face_count: int
    observation: FaceObservation | None
    quality_reason: str | None  # production enrollment quality gate verdict for the chosen face
    aligned: NDArray[np.uint8] | None


class Embedder(Protocol):
    name: str
    model_file: Path

    def embed(self, aligned: NDArray[np.uint8]) -> NDArray[np.float32]: ...


class Acquirer:
    def __init__(self, store: ModelStore, quality: QualityService):
        self.detector = FaceDetector(store.ensure(YUNET))
        self.aligner = cv2.FaceRecognizerSF.create(str(store.ensure(SFACE)), "")
        self.quality = quality

    def acquire(self, path: Path) -> Acquisition:
        loaded = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if loaded is None:
            return Acquisition("DECODE_ERROR", 0, None, None, None)
        image = np.asarray(loaded, dtype=np.uint8)
        height, width = image.shape[:2]
        self.detector.detector.setInputSize((width, height))
        _, faces = self.detector.detector.detect(image)
        if faces is None or len(faces) == 0:
            return Acquisition("NO_FACE", 0, None, None, None)
        # LFW centres the labelled person; with several faces take the one closest to the image centre.
        centre = np.asarray([width / 2.0, height / 2.0])
        raw = min(
            (np.asarray(face, dtype=np.float32) for face in faces),
            key=lambda face: float(np.linalg.norm(face[0:2] + face[2:4] / 2.0 - centre)),
        )
        x, y, w, h = (int(value) for value in raw[:4])
        observation = FaceObservation(
            raw=raw, confidence=float(raw[-1]), box=(x, y, w, h), yaw_proxy=FaceDetector.yaw_from_yunet_landmarks(raw)
        )
        verdict = self.quality.evaluate(image, observation)
        aligned = np.asarray(self.aligner.alignCrop(image, raw), dtype=np.uint8)
        return Acquisition("OK", len(faces), observation, None if verdict.accepted else verdict.reason_code, aligned)


class SFaceEmbedder:
    name = "SFace (OpenCV Zoo, 2021dec)"

    def __init__(self, store: ModelStore):
        self.model_file = store.ensure(SFACE)
        self.recognizer = cv2.FaceRecognizerSF.create(str(self.model_file), "")

    def embed(self, aligned: NDArray[np.uint8]) -> NDArray[np.float32]:
        return normalize(np.asarray(self.recognizer.feature(aligned), dtype=np.float32).reshape(-1))


class ArcFaceEmbedder:
    """ONNX Model Zoo ArcFace LResNet100E-IR (InsightFace). Input: 1x3x112x112 RGB, 0-255.

    The network normalises pixels itself: its first nodes are ``_minusscalar0`` and ``_mulscalar0``.
    """

    name = "ArcFace LResNet100E-IR (ONNX Model Zoo, opset 8)"

    def __init__(self, model_file: Path):
        self.model_file = model_file
        self.net = cv2.dnn.readNetFromONNX(str(model_file))

    def embed(self, aligned: NDArray[np.uint8]) -> NDArray[np.float32]:
        blob = cv2.dnn.blobFromImage(aligned, 1.0, (112, 112), (0, 0, 0), swapRB=True, crop=False)
        self.net.setInput(blob)
        return normalize(np.asarray(self.net.forward(), dtype=np.float32).reshape(-1))


def timed_embed(embedder: Embedder, aligned: NDArray[np.uint8]) -> tuple[NDArray[np.float32], float]:
    started = time.perf_counter()
    embedding = embedder.embed(aligned)
    return embedding, (time.perf_counter() - started) * 1000.0
