"""Passive presentation-attack detection with the MiniFASNet ensemble from Silent-Face-Anti-Spoofing.

Each model sees the face box enlarged by its own scale, resized to 80x80 and fed as raw float32 BGR
(no /255 or mean subtraction), exactly as the reference implementation does. Both models output three
logits where index 1 is a live face; the live score is the softmax probability of that class averaged
over the ensemble.
"""

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np
from numpy.typing import NDArray

from app.core.config import Settings
from app.services.biometrics.errors import BiometricError
from app.services.biometrics.models import FaceObservation

INPUT_SIZE = 80
LIVE_CLASS = 1
# Crop scales the reference MiniFASNetV2 and MiniFASNetV1SE weights were trained with.
MINIFASNET_V2_SCALE = 2.7
MINIFASNET_V1SE_SCALE = 4.0


class SpoofDetectedError(BiometricError):
    def __init__(self, live_score: float, threshold: float):
        super().__init__("SPOOF_DETECTED", "The face looks like a photo, screen or printout")
        self.live_score = live_score
        self.threshold = threshold


class OnnxNet(Protocol):
    def setInput(self, blob: NDArray[np.float32]) -> None: ...  # noqa: N802 -- cv2.dnn API

    def forward(self) -> Any: ...


@dataclass(frozen=True)
class SpoofModel:
    net: OnnxNet
    scale: float


def crop_box(
    box: tuple[int, int, int, int], image_width: int, image_height: int, scale: float
) -> tuple[int, int, int, int]:
    """Enlarge the face box around its centre; shift it back inside the image instead of clipping it.

    Mirrors CropImage._get_new_box from Silent-Face-Anti-Spoofing so the crop matches the training data.
    Returns inclusive (left, top, right, bottom) pixel coordinates.
    """
    x, y, width, height = box
    width, height = max(width, 1), max(height, 1)
    scale = min((image_height - 1) / height, (image_width - 1) / width, scale)
    new_width, new_height = width * scale, height * scale
    center_x, center_y = x + width / 2, y + height / 2
    left, top = center_x - new_width / 2, center_y - new_height / 2
    right, bottom = center_x + new_width / 2, center_y + new_height / 2
    if left < 0:
        right -= left
        left = 0
    if top < 0:
        bottom -= top
        top = 0
    if right > image_width - 1:
        left -= right - image_width + 1
        right = image_width - 1
    if bottom > image_height - 1:
        top -= bottom - image_height + 1
        bottom = image_height - 1
    return int(left), int(top), int(right), int(bottom)


class AntiSpoofingService:
    """Runs after face detection and before template comparison; settings are read on every call."""

    def __init__(self, models: Sequence[SpoofModel], settings: Settings):
        if not models:
            raise ValueError("At least one anti-spoofing model is required")
        self.models = tuple(models)
        self.settings = settings
        # cv2.dnn networks keep per-call state, so concurrent requests must not share one forward pass.
        self._lock = threading.Lock()

    @classmethod
    def from_onnx(cls, models: Sequence[tuple[Path, float]], settings: Settings) -> "AntiSpoofingService":
        loaded = []
        for path, scale in models:
            net = cv2.dnn.readNetFromONNX(str(path))
            net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
            loaded.append(SpoofModel(net, scale))
        return cls(loaded, settings)

    def live_score(self, image: NDArray[np.uint8], observation: FaceObservation) -> float:
        height, width = image.shape[:2]
        probabilities = []
        with self._lock:
            for model in self.models:
                left, top, right, bottom = crop_box(observation.box, width, height, model.scale)
                face = cv2.resize(image[top : bottom + 1, left : right + 1], (INPUT_SIZE, INPUT_SIZE))
                blob = np.ascontiguousarray(face.transpose(2, 0, 1)[np.newaxis], dtype=np.float32)
                model.net.setInput(blob)
                logits = np.asarray(model.net.forward(), dtype=np.float64).reshape(-1)
                exponent = np.exp(logits - logits.max())
                probabilities.append(float(exponent[LIVE_CLASS] / exponent.sum()))
        return float(np.mean(probabilities))

    def check(self, image: NDArray[np.uint8], observation: FaceObservation) -> float | None:
        """Return the live score, or None when the check is switched off; raise on a likely spoof."""
        if not self.settings.anti_spoofing_enabled:
            return None
        score = self.live_score(image, observation)
        threshold = self.settings.anti_spoofing_threshold
        if score < threshold:
            raise SpoofDetectedError(score, threshold)
        return score
