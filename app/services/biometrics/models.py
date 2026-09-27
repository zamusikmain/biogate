from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class FaceObservation:
    raw: NDArray[np.float32]
    confidence: float
    box: tuple[int, int, int, int]
    yaw_proxy: float


@dataclass(frozen=True)
class QualityResult:
    accepted: bool
    score: float
    sharpness: float
    brightness: float
    reason_code: str


@dataclass(frozen=True)
class FrameResult:
    embedding: NDArray[np.float32]
    quality: QualityResult
    observation: FaceObservation


@dataclass(frozen=True)
class LivenessResult:
    passed: bool
    score: float
    reason_code: str
    observed_range: float


@dataclass(frozen=True)
class FrameAnalysis:
    embedding: NDArray[np.float32]
    quality: QualityResult
    observation: FaceObservation
    eye_aspect_ratio: float | None
    image_width: int
    image_height: int
