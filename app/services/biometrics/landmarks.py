from pathlib import Path
from typing import Any

import cv2
import mediapipe as mp
import numpy as np
from numpy.typing import NDArray

from app.services.biometrics.quality import line_roll_degrees

LEFT_EYE = (33, 160, 158, 133, 153, 144)
RIGHT_EYE = (362, 385, 387, 263, 373, 380)
OUTER_EYE_CORNERS = (33, 263)


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
        points = self._landmarks(image)
        if points is None:
            return None
        return self._ear(points, LEFT_EYE), self._ear(points, RIGHT_EYE)

    def roll_degrees(self, image: NDArray[np.uint8]) -> float | None:
        """Head tilt from the outer eye corners; within 1 degree on rotated LFW faces."""
        points = self._landmarks(image)
        if points is None:
            return None
        height, width = image.shape[:2]
        first, second = points[OUTER_EYE_CORNERS[0]], points[OUTER_EYE_CORNERS[1]]
        # Landmarks are normalized per axis; scale back to pixels so non-square images keep the angle.
        return line_roll_degrees((second.x - first.x) * width, (second.y - first.y) * height)

    def _landmarks(self, image: NDArray[np.uint8]) -> Any | None:
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        result: Any = self.landmarker.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
        return result.face_landmarks[0] if result.face_landmarks else None

    @staticmethod
    def _ear(points: Any, indices: tuple[int, int, int, int, int, int]) -> float:
        coords = np.asarray([(points[index].x, points[index].y) for index in indices], dtype=np.float32)
        vertical = np.linalg.norm(coords[1] - coords[5]) + np.linalg.norm(coords[2] - coords[4])
        horizontal = 2.0 * max(float(np.linalg.norm(coords[0] - coords[3])), 1e-6)
        return float(vertical / horizontal)
