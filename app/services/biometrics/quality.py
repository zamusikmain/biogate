import math

import cv2
import numpy as np
from numpy.typing import NDArray

from app.services.biometrics.models import FaceObservation, QualityResult


def head_roll_degrees(raw: NDArray[np.float32]) -> float:
    """Signed in-plane tilt of the line through the two YuNet eye landmarks (indexes 4-7), in degrees."""
    dx, dy = float(raw[6] - raw[4]), float(raw[7] - raw[5])
    angle = math.degrees(math.atan2(dy, dx))
    # Eye order is not guaranteed; fold the angle into [-90, 90].
    if angle > 90:
        angle -= 180
    elif angle < -90:
        angle += 180
    return angle


class EnrollmentPoseGate:
    """Head-pose limits applied only to frames that become part of a stored template."""

    def __init__(self, max_yaw: float, max_roll_degrees: float):
        self.max_yaw = max_yaw
        self.max_roll_degrees = max_roll_degrees

    def evaluate(self, face: FaceObservation) -> str | None:
        if abs(face.yaw_proxy) > self.max_yaw:
            return "HEAD_TURNED"
        if abs(head_roll_degrees(face.raw)) > self.max_roll_degrees:
            return "HEAD_TILTED"
        return None


class QualityService:
    def __init__(
        self,
        min_face_pixels: int,
        min_sharpness: float,
        min_brightness: float,
        max_brightness: float,
    ):
        self.min_face_pixels = min_face_pixels
        self.min_sharpness = min_sharpness
        self.min_brightness = min_brightness
        self.max_brightness = max_brightness

    def evaluate(self, image: NDArray[np.uint8], face: FaceObservation) -> QualityResult:
        x, y, w, h = face.box
        x, y = max(x, 0), max(y, 0)
        crop = image[y : min(y + h, image.shape[0]), x : min(x + w, image.shape[1])]
        if crop.size == 0 or min(w, h) < self.min_face_pixels:
            return QualityResult(False, 0.0, 0.0, 0.0, "FACE_TOO_SMALL")
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        brightness = float(gray.mean())
        sharp_score = min(sharpness / max(self.min_sharpness * 2, 1), 1.0)
        brightness_score = max(0.0, 1.0 - abs(brightness - 130.0) / 130.0)
        score = round(0.65 * sharp_score + 0.35 * brightness_score, 4)
        if sharpness < self.min_sharpness:
            return QualityResult(False, score, sharpness, brightness, "IMAGE_BLURRY")
        if not self.min_brightness <= brightness <= self.max_brightness:
            return QualityResult(False, score, sharpness, brightness, "BAD_LIGHTING")
        return QualityResult(True, score, sharpness, brightness, "OK")
