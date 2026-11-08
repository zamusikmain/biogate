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
    sha256: str
    max_bytes: int = 100 * 1024 * 1024


# URLs point at immutable revisions (an opencv_zoo commit, a versioned MediaPipe path) and every file is
# verified against its SHA-256, so a model can never change silently between installations.
OPENCV_ZOO = "https://github.com/opencv/opencv_zoo/raw"
YUNET = ModelSpec(
    "face_detection_yunet_2023mar.onnx",
    f"{OPENCV_ZOO}/f12e12798e8314f7c074a6656816c048dcc95b7a/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
)
SFACE = ModelSpec(
    "face_recognition_sface_2021dec.onnx",
    f"{OPENCV_ZOO}/ba91a3b91d00d76e86540d4013f944bd6b514e39/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
    "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
)
FACE_LANDMARKER = ModelSpec(
    "face_landmarker.task",
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
    "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff",
    max_bytes=20 * 1024 * 1024,
)


DOWNLOAD_ATTEMPTS = 3


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
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                return self._download(spec, target)
            except OSError as error:  # network errors and timeouts; integrity failures are not retried
                if attempt == DOWNLOAD_ATTEMPTS:
                    raise
                logger.warning("Download of %s failed (%s); retrying", spec.filename, error)
        raise AssertionError("unreachable")

    def _download(self, spec: ModelSpec, target: Path) -> Path:
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
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return digest == spec.sha256
