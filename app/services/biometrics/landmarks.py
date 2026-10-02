from pathlib import Path
from typing import Any

import cv2
import mediapipe as mp
import numpy as np
from numpy.typing import NDArray

LEFT_EYE = (33, 160, 158, 133, 153, 144)
RIGHT_EYE = (362, 385, 387, 263, 373, 380)


class EyeGeometryService:
    """MediaPipe Face Landmarker eye aspect ratio estimator for blink challenges."""

    def __init__(self, model_path: Path):
        options = mp.tasks.vision.FaceLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(model_path)),
            running_mode=mp.tasks.vision.RunningMode.IMAGE,
            num_faces=1,
            min_face_detection_confidence=0.5,
            min_face_presence_confidence=0.5,
        )
        self.landmarker = mp.tasks.vision.FaceLandmarker.create_from_options(options)

    def eye_aspect_ratio(self, image: NDArray[np.uint8]) -> float | None:
        ratios = self.eye_aspect_ratios(image)
        return float(sum(ratios) / 2.0) if ratios else None

    def eye_aspect_ratios(self, image: NDArray[np.uint8]) -> tuple[float, float] | None:
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        result: Any = self.landmarker.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
        if not result.face_landmarks:
            return None
        points = result.face_landmarks[0]
        return self._ear(points, LEFT_EYE), self._ear(points, RIGHT_EYE)

    @staticmethod
    def _ear(points: Any, indices: tuple[int, int, int, int, int, int]) -> float:
        coords = np.asarray([(points[index].x, points[index].y) for index in indices], dtype=np.float32)
        vertical = np.linalg.norm(coords[1] - coords[5]) + np.linalg.norm(coords[2] - coords[4])
        horizontal = 2.0 * max(float(np.linalg.norm(coords[0] - coords[3])), 1e-6)
        return float(vertical / horizontal)
