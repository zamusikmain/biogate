import hashlib
import re
from pathlib import Path

import pytest

from app.services.biometrics.model_store import FACE_LANDMARKER, SFACE, YUNET, ModelSpec, ModelStore

SPECS = [YUNET, SFACE, FACE_LANDMARKER]


@pytest.mark.parametrize("spec", SPECS, ids=lambda spec: spec.filename)
def test_every_model_is_pinned_to_an_immutable_url_and_hash(spec: ModelSpec) -> None:
    assert re.fullmatch(r"[0-9a-f]{64}", spec.sha256)
    assert spec.url.startswith("https://")
    assert "latest" not in spec.url
    assert "refs/heads" not in spec.url


def test_download_is_retried_after_network_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ModelStore(tmp_path)
    attempts: list[int] = []

    def flaky(spec: ModelSpec, target: Path) -> Path:
        attempts.append(1)
        if len(attempts) < 3:
            raise TimeoutError("The read operation timed out")
        return target

    monkeypatch.setattr(store, "_download", flaky)
    assert store.ensure(YUNET) == tmp_path / YUNET.filename
    assert len(attempts) == 3

    attempts.clear()
    monkeypatch.setattr(store, "_download", lambda spec, target: (_ for _ in ()).throw(TimeoutError("down")))
    with pytest.raises(TimeoutError):
        store.ensure(YUNET)


def test_cached_model_with_wrong_hash_is_rejected(tmp_path: Path) -> None:
    data = b"x" * 200_000
    path = tmp_path / "model.onnx"
    path.write_bytes(data)
    good = ModelSpec("model.onnx", "https://example.invalid/model.onnx", hashlib.sha256(data).hexdigest())
    bad = ModelSpec("model.onnx", "https://example.invalid/model.onnx", "0" * 64)
    assert ModelStore._valid(path, good) is True
    assert ModelStore._valid(path, bad) is False
