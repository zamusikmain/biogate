import hashlib
import logging
import os
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelSpec:
    filename: str
    url: str
    sha256: str | None = None
    max_bytes: int = 100 * 1024 * 1024


YUNET = ModelSpec(
    "face_detection_yunet_2023mar.onnx",
    "https://github.com/opencv/opencv_zoo/raw/refs/heads/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
)
SFACE = ModelSpec(
    "face_recognition_sface_2021dec.onnx",
    "https://github.com/opencv/opencv_zoo/raw/refs/heads/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
    "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
)
FACE_LANDMARKER = ModelSpec(
    "face_landmarker.task",
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task",
    max_bytes=20 * 1024 * 1024,
)


class ModelStore:
    def __init__(self, cache: Path):
        self.cache = cache

    def ensure(self, spec: ModelSpec) -> Path:
        if urlparse(spec.url).scheme != "https":
            raise ValueError("Model downloads require HTTPS")
        self.cache.mkdir(parents=True, exist_ok=True)
        target = self.cache / spec.filename
        if target.exists() and self._valid(target, spec):
            return target
        logger.info("Downloading public CV model %s", spec.filename)
        fd, temporary_name = tempfile.mkstemp(prefix="biogate-", suffix=".download", dir=self.cache)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            request = urllib.request.Request(  # noqa: S310 -- URL scheme is restricted above
                spec.url, headers={"User-Agent": "BioGate/0.1"}
            )
            with (
                urllib.request.urlopen(request, timeout=90) as response,  # noqa: S310
                temporary.open("wb") as output,
            ):  # noqa: S310
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > spec.max_bytes:
                        raise ValueError(f"Model {spec.filename} exceeds size limit")
                    output.write(chunk)
            if not self._valid(temporary, spec):
                raise ValueError(f"Integrity check failed for {spec.filename}")
            temporary.replace(target)
            return target
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _valid(path: Path, spec: ModelSpec) -> bool:
        if not path.is_file() or path.stat().st_size < 100_000:
            return False
        if spec.sha256 is None:
            return True
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return digest == spec.sha256
