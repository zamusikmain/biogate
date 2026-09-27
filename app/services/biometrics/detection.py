from pathlib import Path

import cv2
import numpy as np
from numpy.typing import NDArray

from app.services.biometrics.errors import BiometricError
from app.services.biometrics.models import FaceObservation


class FaceDetector:
    def __init__(self, model_path: Path, confidence: float = 0.85):
        self.detector = cv2.FaceDetectorYN.create(
            str(model_path),
            "",
            (320, 320),
            confidence,
            0.3,
            5000,
            cv2.dnn.DNN_BACKEND_OPENCV,
            cv2.dnn.DNN_TARGET_CPU,
        )

    def detect_one(self, image: NDArray[np.uint8]) -> FaceObservation:
        height, width = image.shape[:2]
        self.detector.setInputSize((width, height))
        _, faces = self.detector.detect(image)
        count = 0 if faces is None else len(faces)
        if count == 0:
            raise BiometricError("NO_FACE", "No face was detected")
        if count != 1:
            raise BiometricError("MULTIPLE_FACES", "Exactly one face is required")
        raw = np.asarray(faces[0], dtype=np.float32)
        x, y, w, h = (int(value) for value in raw[:4])
        yaw_proxy = self.yaw_from_yunet_landmarks(raw)
        return FaceObservation(raw=raw, confidence=float(raw[-1]), box=(x, y, w, h), yaw_proxy=yaw_proxy)

    @staticmethod
    def yaw_from_yunet_landmarks(raw: NDArray[np.float32]) -> float:
        """Return a signed horizontal nose offset in the original image coordinates.

        YuNet stores the two eye landmarks at indexes 4 and 6 and the nose at
        index 8.  Eye order is irrelevant because their midpoint and absolute
        distance are used.  Negative values mean image-left, positive values
        mean image-right.  For the front-facing camera, image-right is the
        user's physical left and image-left is the user's physical right. CSS
        preview mirroring never reaches this calculation.
        """
        first_eye_x, second_eye_x, nose_x = float(raw[4]), float(raw[6]), float(raw[8])
        eye_mid = (first_eye_x + second_eye_x) / 2.0
        eye_distance = max(abs(second_eye_x - first_eye_x), 1.0)
        return (nose_x - eye_mid) / eye_distance
