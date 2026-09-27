import cv2
import numpy as np
from numpy.typing import NDArray

from app.core.config import Settings
from app.services.biometrics.detection import FaceDetector
from app.services.biometrics.embedding import EmbeddingService, aggregate, cosine_similarity
from app.services.biometrics.errors import BiometricError
from app.services.biometrics.landmarks import EyeGeometryService
from app.services.biometrics.liveness import LivenessService
from app.services.biometrics.model_store import FACE_LANDMARKER, SFACE, YUNET, ModelStore
from app.services.biometrics.models import FrameAnalysis, FrameResult, LivenessResult
from app.services.biometrics.quality import QualityService


class BiometricPipeline:
    def __init__(self, settings: Settings):
        store = ModelStore(settings.model_cache)
        self.detector = FaceDetector(store.ensure(YUNET))
        self.embedder = EmbeddingService(store.ensure(SFACE))
        self.eye_geometry = EyeGeometryService(store.ensure(FACE_LANDMARKER))
        self.quality = QualityService(
            settings.min_face_pixels,
            settings.min_sharpness,
            settings.min_brightness,
            settings.max_brightness,
        )
        self.liveness = LivenessService()
        self.model_name = self.embedder.model_name
        self.model_version = self.embedder.model_version

    @staticmethod
    def decode(data: bytes) -> NDArray[np.uint8]:
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.ndim != 3:
            raise BiometricError("INVALID_IMAGE", "The upload is not a decodable image")
        if image.shape[0] * image.shape[1] > 16_000_000:
            raise BiometricError("IMAGE_DIMENSIONS_TOO_LARGE", "Image dimensions exceed the limit")
        return np.asarray(image, dtype=np.uint8)

    def process_frame(self, data: bytes) -> FrameResult:
        image = self.decode(data)
        observation = self.detector.detect_one(image)
        quality = self.quality.evaluate(image, observation)
        if not quality.accepted:
            raise BiometricError(quality.reason_code, "Image quality check failed")
        embedding = self.embedder.extract(image, observation)
        return FrameResult(embedding, quality, observation)

    def analyze_frame(self, data: bytes) -> FrameAnalysis:
        image = self.decode(data)
        observation = self.detector.detect_one(image)
        quality = self.quality.evaluate(image, observation)
        if not quality.accepted:
            raise BiometricError(quality.reason_code, "Image quality check failed")
        embedding = self.embedder.extract(image, observation)
        ear = self.eye_geometry.eye_aspect_ratio(image)
        return FrameAnalysis(
            embedding=embedding,
            quality=quality,
            observation=observation,
            eye_aspect_ratio=ear,
            image_width=int(image.shape[1]),
            image_height=int(image.shape[0]),
        )

    def create_template(self, frames: list[bytes]) -> tuple[NDArray[np.float32], float]:
        results = [self.process_frame(frame) for frame in frames]
        return aggregate([result.embedding for result in results]), sum(r.quality.score for r in results) / len(results)

    def verify(self, frames: list[bytes], template: NDArray[np.float32]) -> tuple[float, float, LivenessResult]:
        results = [self.process_frame(frame) for frame in frames]
        liveness = self.liveness.evaluate([result.observation for result in results])
        probe = aggregate([result.embedding for result in results])
        quality = sum(result.quality.score for result in results) / len(results)
        return cosine_similarity(probe, template), quality, liveness
