import os
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services.biometrics.pipeline import BiometricPipeline


@pytest.mark.skipif(
    not os.getenv("BIOGATE_SMOKE_IMAGE"),
    reason="Set BIOGATE_SMOKE_IMAGE to a consented single-face image",
)
def test_real_pipeline_smoke(tmp_path: Path) -> None:
    pipeline = BiometricPipeline(Settings(model_cache=tmp_path / "models"))
    result = pipeline.process_frame(Path(os.environ["BIOGATE_SMOKE_IMAGE"]).read_bytes())
    assert result.embedding.size > 0
    assert result.quality.accepted
