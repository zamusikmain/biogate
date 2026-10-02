import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from tests.test_v3 import v3_admin_login

ROOT = Path(__file__).parents[1]


def test_javascript_runtime_regressions() -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for FaceGuidance runtime regression tests"
    result = subprocess.run(  # noqa: S603
        [node, "--test", str(ROOT / "tests/ui/face-guidance.test.cjs")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_login_loads_guidance_before_controller_and_is_not_cached(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]
    text = response.text
    assert text.index("face-guidance-locales.js?v=5") < text.index("face-guidance.js?v=5")
    assert text.index("face-guidance.js?v=5") < text.index("login-v3.js?v=5")
    for obsolete in ('id="camera-action"', 'id="camera-progress"', 'id="face-result"'):
        assert obsolete not in text
    assert client.get("/__qa/report").status_code == 404
    assert client.get("/__qa/harness.js").status_code == 404


def test_face_login_geometry_is_observational_and_center_remains_server_owned(client: TestClient) -> None:
    created = client.post("/api/v3/face/challenges").json()
    result = client.post(
        f"/api/v3/face/challenges/{created['challenge_id']}/frames",
        files={"frame": ("frame.jpg", b"face", "image/jpeg")},
    )
    assert result.status_code == 200
    body = result.json()
    assert body["face_geometry"] == {
        "x": 10, "y": 10, "width": 100, "height": 100, "image_width": 320, "image_height": 240,
    }
    assert body["accepted"] is False
    assert body["observed_action"] == "CALIBRATING"


def test_user_frame_feedback_requires_session_csrf_and_does_not_enroll(client: TestClient) -> None:
    photo = {"photo": ("frame.jpg", b"face", "image/jpeg")}
    assert client.post("/api/v3/user/biometric-frame", files=photo).status_code == 401
    v3_admin_login(client)
    token = client.headers.pop("X-CSRF-Token")
    assert client.post("/api/v3/user/biometric-frame", files=photo).status_code == 403
    client.headers["X-CSRF-Token"] = token
    before = client.get("/api/v3/user/biometric-request").json()
    valid = client.post("/api/v3/user/biometric-frame", files=photo)
    assert valid.status_code == 200
    assert valid.json()["accepted"] is True
    for raw, reason in [(b"noface", "NO_FACE"), (b"multiple", "MULTIPLE_FACES"), (b"blurry", "IMAGE_BLURRY")]:
        result = client.post("/api/v3/user/biometric-frame", files={"photo": ("frame.jpg", raw, "image/jpeg")})
        assert result.json() == {"accepted": False, "reason_code": reason}
    assert client.get("/api/v3/user/biometric-request").json() == before


def test_pose_guidance_uses_existing_policy_without_advancing_challenge(client: TestClient) -> None:
    service = client.app.state.face_login_service
    analysis = client.app.state.pipeline.analyze_frame(b"face")
    state = service.create()
    excessive = replace(analysis, observation=replace(analysis.observation, yaw_proxy=0.3))
    result = service.analyze(state["challenge_id"], excessive, b"face")
    assert result["guidance_reason"] == "POSE_NOT_CENTERED"
    assert result["accepted"] is False and result["current_step"] == 0
    state = service.create()
    for yaw in (0.0, 0.10, -0.10):
        result = service.analyze(
            state["challenge_id"], replace(analysis, observation=replace(analysis.observation, yaw_proxy=yaw)), b"face"
        )
    assert result["guidance_reason"] == "UNSTABLE"
    assert result["accepted"] is False and result["current_step"] == 0
