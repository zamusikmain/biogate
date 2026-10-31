import json
import re
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from numpy.typing import NDArray

from app.core.config import Settings
from app.services.account_auth import ACCOUNT_COOKIE_NAME
from app.services.biometrics.anti_spoofing import (
    MINIFASNET_V1SE_SCALE,
    MINIFASNET_V2_SCALE,
    AntiSpoofingService,
    SpoofDetectedError,
    SpoofModel,
    crop_box,
)
from app.services.biometrics.errors import BiometricError
from app.services.biometrics.model_store import MINIFASNET_V1SE, MINIFASNET_V2, ModelSpec, ModelStore
from app.services.biometrics.models import FaceObservation, QualityResult
from app.services.biometrics.pipeline import BiometricPipeline
from app.services.biometrics.quality import EnrollmentPoseGate
from app.services.spoof_events import SpoofEventRecorder
from tests.test_v3 import v3_admin_login

LIVE_LOGITS = [-3.0, 4.0, -2.0]  # softmax(live) ~ 0.999
SPOOF_LOGITS = [4.0, -3.0, 1.0]  # softmax(live) ~ 0.001
MODEL_CACHE = Path("model_cache")


class FakeNet:
    def __init__(self, logits: list[float]):
        self.logits = np.asarray([logits], dtype=np.float32)
        self.inputs: list[NDArray[np.float32]] = []

    def setInput(self, blob: NDArray[np.float32]) -> None:  # noqa: N802 -- cv2.dnn API
        self.inputs.append(blob)

    def forward(self) -> NDArray[np.float32]:
        return self.logits


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def _service(logits: list[float], settings: Settings | None = None) -> tuple[AntiSpoofingService, list[FakeNet]]:
    nets = [FakeNet(logits), FakeNet(logits)]
    models = [SpoofModel(nets[0], MINIFASNET_V2_SCALE), SpoofModel(nets[1], MINIFASNET_V1SE_SCALE)]
    return AntiSpoofingService(models, settings or _settings()), nets


def _observation() -> FaceObservation:
    return FaceObservation(np.zeros(15, dtype=np.float32), 0.99, (60, 40, 80, 100), 0.0)


def _image() -> NDArray[np.uint8]:
    return np.full((240, 320, 3), 128, dtype=np.uint8)


# --- model store -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("spec", [MINIFASNET_V2, MINIFASNET_V1SE], ids=lambda spec: spec.filename)
def test_anti_spoofing_models_are_pinned_by_https_url_and_sha256(spec: ModelSpec) -> None:
    assert re.fullmatch(r"[0-9a-f]{64}", spec.sha256)
    assert spec.url.startswith("https://")
    assert spec.url.endswith(spec.filename)
    assert "latest" not in spec.url


def test_tampered_anti_spoofing_model_is_rejected(tmp_path: Path) -> None:
    (tmp_path / MINIFASNET_V2.filename).write_bytes(b"x" * 200_000)
    assert ModelStore._valid(tmp_path / MINIFASNET_V2.filename, MINIFASNET_V2) is False


@pytest.mark.skipif(
    not all((MODEL_CACHE / spec.filename).is_file() for spec in (MINIFASNET_V2, MINIFASNET_V1SE)),
    reason="MiniFASNet models are not in model_cache yet; start the app once to download them",
)
def test_real_minifasnet_models_pass_integrity_check_and_load_on_cpu() -> None:
    store = ModelStore(MODEL_CACHE)
    for spec in (MINIFASNET_V2, MINIFASNET_V1SE):
        assert ModelStore._valid(MODEL_CACHE / spec.filename, spec), f"{spec.filename} does not match its SHA-256"
    service = AntiSpoofingService.from_onnx(
        [
            (store.ensure(MINIFASNET_V2), MINIFASNET_V2_SCALE),
            (store.ensure(MINIFASNET_V1SE), MINIFASNET_V1SE_SCALE),
        ],
        _settings(),
    )
    score = service.live_score(_image(), _observation())
    assert 0.0 <= score <= 1.0


# --- service -----------------------------------------------------------------------------------------------------


def test_crop_box_scales_around_the_face_and_stays_inside_the_image() -> None:
    assert crop_box((100, 100, 40, 40), 320, 240, 2.0) == (80, 80, 160, 160)
    # A face near the corner is shifted inwards, not clipped, so the crop keeps its size.
    left, top, right, bottom = crop_box((0, 0, 40, 40), 320, 240, 2.7)
    assert (left, top) == (0, 0) and right - left == 108 and bottom - top == 108
    # The scale is capped when the enlarged box would not fit the image at all.
    left, top, right, bottom = crop_box((60, 40, 80, 100), 320, 240, 4.0)
    assert top >= 0 and bottom <= 239 and left >= 0 and right <= 319


def test_live_face_passes_and_model_input_matches_minifasnet_preprocessing() -> None:
    service, nets = _service(LIVE_LOGITS)
    score = service.check(_image(), _observation())
    assert score is not None and score > 0.99
    for net in nets:
        (blob,) = net.inputs
        assert blob.shape == (1, 3, 80, 80) and blob.dtype == np.float32
        assert blob.max() == pytest.approx(128.0)  # raw BGR values, no /255 normalisation


def test_spoof_is_rejected_with_score_and_threshold() -> None:
    service, _ = _service(SPOOF_LOGITS)
    with pytest.raises(SpoofDetectedError) as error:
        service.check(_image(), _observation())
    assert error.value.reason_code == "SPOOF_DETECTED"
    assert isinstance(error.value, BiometricError)
    assert error.value.live_score < 0.01
    assert error.value.threshold == 0.80


def test_threshold_and_switch_are_read_from_settings_on_every_call() -> None:
    settings = _settings()
    service, nets = _service([0.0, 1.0, 0.0], settings)  # live probability ~ 0.58
    with pytest.raises(SpoofDetectedError):
        service.check(_image(), _observation())
    settings.anti_spoofing_threshold = 0.5
    assert service.check(_image(), _observation()) == pytest.approx(0.576, abs=0.001)
    settings.anti_spoofing_enabled = False
    calls = len(nets[0].inputs)
    assert service.check(_image(), _observation()) is None
    assert len(nets[0].inputs) == calls  # switched off means the models do not run at all


def test_default_threshold_is_strict_and_range_is_validated() -> None:
    settings = _settings()
    assert settings.anti_spoofing_enabled is True
    assert settings.anti_spoofing_threshold == 0.80
    for invalid in (0.3, 1.0):
        with pytest.raises(ValueError):
            _settings(anti_spoofing_threshold=invalid)


# --- pipeline ------------------------------------------------------------------------------------------------------


def _pipeline(logits: list[float]) -> tuple[BiometricPipeline, list[str]]:
    calls: list[str] = []
    pipeline = object.__new__(BiometricPipeline)
    pipeline.decode = lambda data: _image()  # type: ignore[method-assign]
    pipeline.detector = type("Detector", (), {"detect_one": lambda self, _: _observation()})()
    pipeline.quality = type(
        "Quality", (), {"evaluate": lambda self, _, __: QualityResult(True, 0.9, 100.0, 128.0, "OK")}
    )()

    def extract(_: object, __: object) -> NDArray[np.float32]:
        calls.append("embedding")
        return np.asarray([1.0, 0.0], dtype=np.float32)

    pipeline.embedder = type("Embedder", (), {"extract": staticmethod(extract)})()
    pipeline.eye_geometry = type("Eyes", (), {"eye_aspect_ratios": staticmethod(lambda _: (0.3, 0.3))})()
    pipeline.pose_gate = EnrollmentPoseGate(0.35, 20.0)
    pipeline.anti_spoofing = _service(logits)[0]
    return pipeline, calls


@pytest.mark.parametrize(
    "operation",
    [
        lambda pipeline: pipeline.process_frame(b"frame"),
        lambda pipeline: pipeline.process_enrollment_frame(b"frame"),
        lambda pipeline: pipeline.create_template([b"frame", b"frame"]),
        lambda pipeline: pipeline.analyze_frame(b"frame"),
        lambda pipeline: pipeline.analyze_liveness_frame(b"frame"),
        lambda pipeline: pipeline.extract_embedding(b"frame"),
    ],
    ids=["verify", "enrollment", "template", "analyze", "liveness", "embedding"],
)
def test_pipeline_rejects_spoof_before_any_embedding_is_extracted(operation: Any) -> None:
    pipeline, calls = _pipeline(SPOOF_LOGITS)
    with pytest.raises(SpoofDetectedError):
        operation(pipeline)
    assert calls == []


def test_pipeline_passes_live_face_through_to_the_embedding() -> None:
    pipeline, calls = _pipeline(LIVE_LOGITS)
    assert pipeline.process_enrollment_frame(b"frame").embedding.size == 2
    assert pipeline.analyze_frame(b"frame").embedding.size == 2
    assert calls == ["embedding", "embedding"]


# --- API and security log -------------------------------------------------------------------------------------------


def _security_events(client: TestClient, event_type: str = "SPOOF_DETECTED") -> list[dict[str, Any]]:
    rows = client.app.state.repository.security_events(limit=100)  # type: ignore[attr-defined]
    return [row for row in rows if row["event_type"] == event_type]


def _spoof_pipeline(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def spoof(*_: Any, **__: Any) -> Any:
        raise SpoofDetectedError(0.12, 0.8)

    pipeline = client.app.state.pipeline  # type: ignore[attr-defined]
    for name in ("analyze_liveness_frame", "process_enrollment_frame", "process_frame", "create_template"):
        monkeypatch.setattr(pipeline, name, spoof)


def test_face_login_spoof_is_denied_logged_and_never_creates_a_session(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    challenge = client.post("/api/v3/face/challenges").json()
    # A live frame is processed normally before the attack.
    live = client.post(
        f"/api/v3/face/challenges/{challenge['challenge_id']}/frames",
        files={"frame": ("f.jpg", b"face", "image/jpeg")},
    )
    assert live.status_code == 200

    _spoof_pipeline(client, monkeypatch)
    url = f"/api/v3/face/challenges/{challenge['challenge_id']}/frames"
    response = client.post(url, files={"frame": ("f.jpg", b"photo", "image/jpeg")})
    assert response.status_code == 403
    assert response.json()["detail"]["reason_code"] == "SPOOF_DETECTED"
    assert ACCOUNT_COOKIE_NAME not in response.cookies
    assert "set-cookie" not in response.headers

    # The challenge is gone, so the attacker cannot keep going with live frames.
    monkeypatch.undo()
    assert client.post(url, files={"frame": ("f.jpg", b"face", "image/jpeg")}).status_code == 404

    (event,) = _security_events(client)
    assert event["severity"] == "WARNING"
    metadata = json.loads(event["metadata"])
    assert metadata == {
        "context": "FACE_LOGIN",
        "live_score": 0.12,
        "threshold": 0.8,
        "challenge_id": challenge["challenge_id"],
    }
    access = client.app.state.repository.access_events(limit=10)  # type: ignore[attr-defined]
    denied = [row for row in access if row["reason"] == "SPOOF_DETECTED"]
    assert len(denied) == 1 and denied[0]["result"] == "DENIED" and denied[0]["session_id"] is None
    assert event["access_event_id"] == denied[0]["id"]
    with client.app.state.repository.db.connect() as conn:  # type: ignore[attr-defined]
        sessions = conn.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0]
    assert sessions == 0


def test_recovery_spoof_is_logged_as_password_recovery(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    challenge = client.post("/api/v3/recovery/challenges", json={"login": "nobody"}).json()
    _spoof_pipeline(client, monkeypatch)
    response = client.post(
        f"/api/v3/face/challenges/{challenge['challenge_id']}/frames",
        files={"frame": ("f.jpg", b"photo", "image/jpeg")},
    )
    assert response.status_code == 403
    assert "reset_authorization" not in response.text
    (event,) = _security_events(client)
    assert json.loads(event["metadata"])["context"] == "PASSWORD_RECOVERY"


def test_biometric_registration_spoof_is_rejected_and_logged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    v3_admin_login(client)
    _spoof_pipeline(client, monkeypatch)

    for _ in range(3):  # a held-up photo during live capture logs one event, not one per frame
        preview = client.post("/api/v3/user/biometric-frame", files={"photo": ("f.jpg", b"photo", "image/jpeg")})
        assert preview.json() == {"accepted": False, "reason_code": "SPOOF_DETECTED"}
    assert [json.loads(row["metadata"])["context"] for row in _security_events(client)] == ["BIOMETRIC_CAPTURE"]

    photos = [("photos", (f"{index}.jpg", b"photo", "image/jpeg")) for index in range(3)]
    submitted = client.post("/api/v3/user/biometric-requests", files=photos)
    assert submitted.status_code == 422
    assert submitted.json()["detail"]["reason_code"] == "SPOOF_DETECTED"
    contexts = [json.loads(row["metadata"])["context"] for row in _security_events(client)]
    assert sorted(contexts) == ["BIOMETRIC_CAPTURE", "BIOMETRIC_REQUEST"]
    assert client.get("/api/v3/user/biometric-request").json() in (None, {})


def test_admin_enrollment_spoof_is_rejected_per_photo_and_logged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = client.post("/api/admin/users", json={"external_id": "bob", "display_name": "Bob"})
    assert created.status_code in (200, 201)
    _spoof_pipeline(client, monkeypatch)
    photos = [("photos", (f"{index}.jpg", b"photo", "image/jpeg")) for index in range(3)]
    response = client.post("/api/admin/users/bob/enrollment/photos", files=photos)
    assert response.status_code == 200
    body = response.json()
    assert body["template_created"] is False
    assert {result["reason_code"] for result in body["results"]} == {"SPOOF_DETECTED"}
    assert len(_security_events(client)) == 3


def test_spoof_events_are_visible_in_admin_security_log(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    challenge = client.post("/api/v3/face/challenges").json()
    _spoof_pipeline(client, monkeypatch)
    client.post(
        f"/api/v3/face/challenges/{challenge['challenge_id']}/frames",
        files={"frame": ("f.jpg", b"photo", "image/jpeg")},
    )
    monkeypatch.undo()
    v3_admin_login(client)
    rows = client.get("/api/v3/admin/security-events", params={"limit": 100}).json()
    items = rows["items"] if isinstance(rows, dict) else rows
    spoof = [row for row in items if row["event_type"] == "SPOOF_DETECTED"]
    assert len(spoof) == 1
    assert spoof[0]["metadata"]["context"] == "FACE_LOGIN"


def test_admin_settings_expose_and_update_anti_spoofing(client: TestClient) -> None:
    v3_admin_login(client)
    current = client.get("/api/v3/admin/settings").json()
    assert current["anti_spoofing_enabled"] is True
    assert current["anti_spoofing_threshold"] == 0.80

    updated = client.put(
        "/api/v3/admin/settings", json={"anti_spoofing_enabled": False, "anti_spoofing_threshold": 0.9}
    )
    assert updated.status_code == 200
    assert updated.json()["anti_spoofing_enabled"] is False
    assert updated.json()["anti_spoofing_threshold"] == 0.9
    settings = client.app.state.settings  # type: ignore[attr-defined]
    assert settings.anti_spoofing_enabled is False and settings.anti_spoofing_threshold == 0.9
    repository = client.app.state.repository  # type: ignore[attr-defined]
    assert repository.get_setting("v3_anti_spoofing_threshold") == "0.9"

    for invalid in (0.2, 1.5):
        rejected = client.put("/api/v3/admin/settings", json={"anti_spoofing_threshold": invalid})
        assert rejected.status_code == 422


def test_admin_ui_and_locales_cover_the_new_code() -> None:
    static = Path("app/static")
    template = Path("app/templates/admin.html").read_text(encoding="utf-8")
    assert 'name="anti_spoofing_enabled"' in template and 'name="anti_spoofing_threshold"' in template
    admin_v3 = (static / "admin-v3.js").read_text(encoding="utf-8")
    assert "anti_spoofing_enabled: form.anti_spoofing_enabled.checked" in admin_v3
    for path, keys in {
        "locales.js": ("SPOOF_DETECTED:", "antiSpoofing:", "antiSpoofingThreshold:", "spoofContext:", "FACE_LOGIN:"),
        "v3-locales.js": ("SPOOF_DETECTED:",),
        "face-guidance-locales.js": ("spoof:", "spoofDetail:"),
    }.items():
        text = (static / path).read_text(encoding="utf-8")
        for key in keys:
            assert text.count(key) >= 2, f"{key} needs both RU and EN in {path}"


def test_preview_events_are_throttled_per_user_and_context() -> None:
    written: list[tuple[str, int | None]] = []

    class Repository:
        def add_security_event(self, event_type: str, severity: str, user_id: int | None, *_: Any) -> int:
            written.append((event_type, user_id))
            return len(written)

        def add_notification(self, *_: Any) -> int:
            return 0

    recorder = SpoofEventRecorder(Repository(), cooldown_seconds=60)  # type: ignore[arg-type]
    error = SpoofDetectedError(0.1, 0.8)
    assert recorder.record(error, "BIOMETRIC_CAPTURE", 1, preview=True) == 1
    assert recorder.record(error, "BIOMETRIC_CAPTURE", 1, preview=True) is None
    assert recorder.record(error, "BIOMETRIC_CAPTURE", 2, preview=True) == 2
    assert recorder.record(error, "FACE_LOGIN", 1) == 3  # real attempts are never throttled
    assert recorder.record(error, "FACE_LOGIN", 1) == 4
