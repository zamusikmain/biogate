import json
import secrets
import statistics
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

from app.core.config import Settings
from app.services.biometrics.embedding import aggregate
from app.services.biometrics.models import FrameAnalysis
from app.services.identification import IdentificationService
from app.services.repository import Repository

ACTIONS = ("CENTER", "TURN_LEFT", "TURN_RIGHT", "BLINK")


@dataclass
class BlinkTemporalState:
    phase: str = "WAITING_OPEN"
    consecutive_frames: int = 0

    def reset(self) -> None:
        self.phase = "WAITING_OPEN"
        self.consecutive_frames = 0


class BlinkStateMachine:
    def __init__(
        self,
        open_threshold: float = 0.21,
        closed_threshold: float = 0.16,
        confirmation_frames: int = 2,
        closed_confirmation_frames: int = 1,
    ):
        self.open_threshold = open_threshold
        self.closed_threshold = closed_threshold
        self.confirmation_frames = confirmation_frames
        self.closed_confirmation_frames = closed_confirmation_frames

    def advance(
        self,
        state: BlinkTemporalState,
        eye_aspect_ratio: float | None,
        left_eye_aspect_ratio: float | None = None,
        right_eye_aspect_ratio: float | None = None,
        *,
        centered: bool = True,
    ) -> tuple[str, bool]:
        if state.phase == "COMPLETE":
            return state.phase, True
        if not centered:
            state.reset()
            return state.phase, False
        if eye_aspect_ratio is None:
            state.consecutive_frames = 0
            return state.phase, False
        left = eye_aspect_ratio if left_eye_aspect_ratio is None else left_eye_aspect_ratio
        right = eye_aspect_ratio if right_eye_aspect_ratio is None else right_eye_aspect_ratio
        eyes_open = left >= self.open_threshold and right >= self.open_threshold
        eyes_closed = left <= self.closed_threshold and right <= self.closed_threshold
        expected = eyes_open if state.phase in {"WAITING_OPEN", "WAITING_REOPEN"} else eyes_closed
        required_frames = (
            self.closed_confirmation_frames if state.phase == "WAITING_CLOSED" else self.confirmation_frames
        )
        state.consecutive_frames = state.consecutive_frames + 1 if expected else 0
        if state.consecutive_frames < required_frames:
            return state.phase, False
        state.consecutive_frames = 0
        if state.phase == "WAITING_OPEN":
            state.phase = "WAITING_CLOSED"
        elif state.phase == "WAITING_CLOSED":
            state.phase = "WAITING_REOPEN"
        else:
            state.phase = "COMPLETE"
            return state.phase, True
        return state.phase, False


@dataclass
class EphemeralCapture:
    embeddings: list[NDArray[np.float32]] = field(default_factory=list)
    quality_scores: list[float] = field(default_factory=list)
    stability_step: int = -1
    stability_count: int = 0
    snapshot: bytes | None = None
    inference_time_ms: float = 0.0
    baseline_yaw: float | None = None
    baseline_samples: list[float] = field(default_factory=list)
    blink: BlinkTemporalState = field(default_factory=BlinkTemporalState)
    frame_count: int = 0
    first_frame_at: float | None = None

    def record_frame(self) -> None:
        self.frame_count += 1
        self.first_frame_at = self.first_frame_at or time.perf_counter()

    def effective_fps(self) -> float | None:
        if self.first_frame_at is None or self.frame_count < 2:
            return None
        elapsed = time.perf_counter() - self.first_frame_at
        return self.frame_count / elapsed if elapsed > 0 else None


@dataclass(frozen=True)
class PoseEvaluation:
    detected_pose: str
    yaw_delta: float | None
    matches: bool


class ChallengeService:
    """Server-owned challenge state; biometric samples remain memory-only."""

    def __init__(self, repository: Repository, settings: Settings, identification: IdentificationService | None = None):
        self.repository = repository
        self.settings = settings
        self.identification = identification or IdentificationService(repository, settings)
        self.blink = BlinkStateMachine()
        self._captures: dict[str, EphemeralCapture] = {}
        self._lock = threading.RLock()

    def create(self, external_id: str) -> dict[str, Any]:
        user = self.repository.get_user(external_id)
        if not user:
            raise ValueError("USER_NOT_FOUND")
        if not self.repository.get_template(int(user["id"])):
            raise ValueError("BIOMETRIC_NOT_ENROLLED")
        if user.get("status") == "BLOCKED":
            raise PermissionError("ACCOUNT_BLOCKED")
        tail = ["TURN_LEFT", "TURN_RIGHT", "BLINK"]
        secrets.SystemRandom().shuffle(tail)
        sequence = ["CENTER", *tail]
        now = datetime.now(UTC)
        session_id = str(uuid.uuid4())
        session = self.repository.create_session(
            session_id=session_id,
            user_id=int(user["id"]),
            external_id=external_id,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=self.settings.challenge_ttl_seconds)).isoformat(),
            sequence=sequence,
        )
        with self._lock:
            self._captures[session_id] = EphemeralCapture()
        self.repository.add_audit("VERIFICATION_STARTED", int(user["id"]), "STARTED", {"challenge_id": session_id})
        return session

    def expected_action(self, session_id: str) -> str:
        session = self.repository.get_session(session_id)
        if not session:
            raise KeyError("CHALLENGE_NOT_FOUND")
        sequence = json.loads(session["expected_sequence"])
        index = int(session["current_step"])
        return str(sequence[index]) if index < len(sequence) else "COMPLETE"

    def analyze(
        self,
        session_id: str,
        analysis: FrameAnalysis,
        frame_data: bytes,
        inference_time_ms: float = 0.0,
        embedding_factory: Callable[[], NDArray[np.float32]] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            session = self.repository.get_session(session_id)
            if not session:
                raise KeyError("CHALLENGE_NOT_FOUND")
            if session["state"] != "ACTIVE":
                raise ValueError("CHALLENGE_NOT_ACTIVE")
            now = datetime.now(UTC)
            if now >= datetime.fromisoformat(session["expires_at"]):
                self._reject(session, "CHALLENGE_TIMEOUT", "EXPIRED")
                raise TimeoutError("CHALLENGE_TIMEOUT")
            sequence = json.loads(session["expected_sequence"])
            index = int(session["current_step"])
            if index >= len(sequence):
                self._reject(session, "INVALID_CHALLENGE_SEQUENCE", "FAILED")
                raise ValueError("INVALID_CHALLENGE_SEQUENCE")
            action = str(sequence[index])
            capture = self._captures.setdefault(session_id, EphemeralCapture())
            capture.record_frame()
            capture.inference_time_ms += inference_time_ms
            accepted = False
            feedback = "HOLD_STILL"
            if action == "BLINK":
                centered = (
                    capture.baseline_yaw is not None
                    and abs(analysis.observation.yaw_proxy - capture.baseline_yaw)
                    <= self.settings.head_center_dead_zone
                )
                phase, accepted = self.blink.advance(
                    capture.blink,
                    analysis.eye_aspect_ratio,
                    analysis.left_eye_aspect_ratio,
                    analysis.right_eye_aspect_ratio,
                    centered=centered,
                )
                self.repository.update_session_progress(session_id, index, phase, None)
                feedback = f"BLINK_{phase}"
                detected_pose = "BLINK"
                yaw_delta = (
                    analysis.observation.yaw_proxy - capture.baseline_yaw if capture.baseline_yaw is not None else None
                )
            else:
                pose = self._evaluate_pose(capture, action, analysis.observation.yaw_proxy)
                matches = pose.matches
                detected_pose = pose.detected_pose
                yaw_delta = pose.yaw_delta
                if capture.stability_step != index:
                    capture.stability_step, capture.stability_count = index, 0
                capture.stability_count = capture.stability_count + 1 if matches else 0
                # CENTER is already stable only after the complete calibration window.
                required = 1 if action == "CENTER" else self.settings.head_stability_frames
                accepted = capture.stability_count >= required and self._cooldown_elapsed(session, now)
                feedback = "STABLE" if matches else "ADJUST_POSE"
            if accepted:
                capture.embeddings.append(embedding_factory() if embedding_factory else analysis.embedding)
                capture.quality_scores.append(analysis.quality.score)
                if capture.snapshot is None and action == "CENTER":
                    capture.snapshot = frame_data
                index += 1
                completed = index >= len(sequence)
                self.repository.update_session_progress(
                    session_id,
                    index,
                    "WAITING_OPEN",
                    now.isoformat(),
                    state="CAPTURED" if completed else "ACTIVE",
                )
                if completed:
                    return self._finalize(session, sequence, capture, analysis)
            return self._response(
                session_id,
                sequence,
                index,
                detected_pose,
                analysis,
                accepted,
                feedback,
                expected_pose=action,
                baseline_yaw=capture.baseline_yaw,
                yaw_delta=yaw_delta,
                stability_count=capture.stability_count,
            )

    def expire(self, session_id: str) -> None:
        session = self.repository.get_session(session_id)
        if session and session["state"] == "ACTIVE":
            self._reject(session, "CHALLENGE_CANCELLED", "CANCELLED")

    def _finalize(
        self,
        session: dict[str, Any],
        sequence: list[str],
        capture: EphemeralCapture,
        analysis: FrameAnalysis,
    ) -> dict[str, Any]:
        template_record = self.repository.get_template(int(session["user_id"]))
        if not template_record or len(capture.embeddings) != len(sequence):
            self._reject(session, "INVALID_CHALLENGE_SEQUENCE", "FAILED")
            raise ValueError("INVALID_CHALLENGE_SEQUENCE")
        similarity = self.identification.identity_score(aggregate(capture.embeddings), template_record)
        quality = float(np.mean(capture.quality_scores))
        result = "VERIFIED" if similarity >= self.settings.verification_threshold else "REJECTED"
        reason = "MATCH" if result == "VERIFIED" else "FACE_MISMATCH"
        attempt_id = self.repository.add_verification(
            {
                "user_id": session["user_id"],
                "claimed_external_id": session["claimed_external_id"],
                "timestamp": datetime.now(UTC).isoformat(),
                "face_detected": 1,
                "quality_score": quality,
                "liveness_score": 1.0,
                "similarity_score": similarity,
                "threshold": self.settings.verification_threshold,
                "result": result,
                "reason_code": reason,
                "inference_time_ms": round(capture.inference_time_ms, 2),
                "model_name": template_record["model_name"],
                "model_version": template_record["model_version"],
                "challenge_id": session["id"],
                "challenge_steps": json.dumps(sequence),
                "challenge_result": "PASSED",
            }
        )
        self.repository.finish_session(session["id"], "COMPLETED", result, reason)
        if self._snapshot_enabled() and capture.snapshot:
            self._save_snapshot(attempt_id, capture.snapshot)
        self._captures.pop(session["id"], None)
        response = self._response(
            session["id"],
            sequence,
            len(sequence),
            "COMPLETE",
            analysis,
            True,
            "COMPLETE",
            expected_pose="COMPLETE",
            baseline_yaw=capture.baseline_yaw,
            yaw_delta=(
                analysis.observation.yaw_proxy - capture.baseline_yaw if capture.baseline_yaw is not None else None
            ),
            stability_count=capture.stability_count,
        )
        response.update(
            {
                "completed": True,
                "attempt_id": attempt_id,
                "result": result,
                "reason_code": reason,
                "similarity_score": similarity,
                "threshold": self.settings.verification_threshold,
                "quality_score": quality,
                "liveness_score": 1.0,
                "inference_time_ms": round(capture.inference_time_ms, 2),
            }
        )
        return response

    def _reject(self, session: dict[str, Any], reason: str, state: str) -> int:
        capture = self._captures.pop(session["id"], EphemeralCapture())
        template_record = self.repository.get_template(int(session["user_id"]))
        sequence = json.loads(session["expected_sequence"])
        attempt_id = self.repository.add_verification(
            {
                "user_id": session["user_id"],
                "claimed_external_id": session["claimed_external_id"],
                "timestamp": datetime.now(UTC).isoformat(),
                "face_detected": int(bool(capture.embeddings)),
                "quality_score": float(np.mean(capture.quality_scores)) if capture.quality_scores else None,
                "liveness_score": 0.0,
                "similarity_score": None,
                "threshold": self.settings.verification_threshold,
                "result": "REJECTED",
                "reason_code": reason,
                "inference_time_ms": round(capture.inference_time_ms, 2),
                "model_name": template_record["model_name"] if template_record else None,
                "model_version": template_record["model_version"] if template_record else None,
                "challenge_id": session["id"],
                "challenge_steps": json.dumps(sequence),
                "challenge_result": reason,
            }
        )
        self.repository.finish_session(session["id"], state, "REJECTED", reason)
        if self._snapshot_enabled() and capture.snapshot:
            self._save_snapshot(attempt_id, capture.snapshot)
        return attempt_id

    def _evaluate_pose(self, capture: EphemeralCapture, action: str, yaw: float) -> PoseEvaluation:
        if action == "CENTER" and capture.baseline_yaw is None:
            capture.baseline_samples.append(yaw)
            capture.baseline_samples = capture.baseline_samples[-self.settings.head_baseline_frames :]
            stable = (
                len(capture.baseline_samples) >= self.settings.head_baseline_frames
                and max(capture.baseline_samples) - min(capture.baseline_samples)
                <= self.settings.head_calibration_range
                and abs(statistics.median(capture.baseline_samples)) <= self.settings.head_max_center_yaw
            )
            if stable:
                capture.baseline_yaw = float(statistics.median(capture.baseline_samples))
                return PoseEvaluation("CENTER", 0.0, True)
            return PoseEvaluation("CALIBRATING", None, False)

        if capture.baseline_yaw is None:
            return PoseEvaluation("UNCALIBRATED", None, False)
        delta = yaw - capture.baseline_yaw
        if abs(delta) <= self.settings.head_center_dead_zone:
            detected = "CENTER"
        # YuNet's yaw proxy stays in the original, unmirrored image coordinates:
        # image-right is the user's physical left for a front-facing camera.
        # Keep that diagnostic sign, but map it to the user's physical direction.
        elif delta >= self.settings.head_turn_delta:
            detected = "TURN_LEFT"
        elif delta <= -self.settings.head_turn_delta:
            detected = "TURN_RIGHT"
        else:
            detected = "BETWEEN_POSES"
        return PoseEvaluation(detected, delta, detected == action)

    def _cooldown_elapsed(self, session: dict[str, Any], now: datetime) -> bool:
        last = session.get("last_capture_at")
        return (
            not last
            or (now - datetime.fromisoformat(last)).total_seconds() * 1000 >= self.settings.challenge_cooldown_ms
        )

    def _snapshot_enabled(self) -> bool:
        stored = self.repository.get_setting("store_attempt_images")
        return self.settings.store_attempt_images if stored is None else stored.lower() == "true"

    def _save_snapshot(self, attempt_id: int, data: bytes) -> None:
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return
        self.settings.attempt_image_dir.mkdir(parents=True, exist_ok=True)
        name = f"{uuid.uuid4()}.jpg"
        path = self.settings.attempt_image_dir / name
        if cv2.imwrite(str(path), image, [cv2.IMWRITE_JPEG_QUALITY, 88]):
            now = datetime.now(UTC)
            self.repository.add_snapshot(
                attempt_id,
                name,
                now.isoformat(),
                (now + timedelta(days=self.settings.attempt_image_retention_days)).isoformat(),
            )

    def _response(
        self,
        session_id: str,
        sequence: list[str],
        index: int,
        action: str,
        analysis: FrameAnalysis,
        accepted: bool,
        feedback: str,
        *,
        expected_pose: str,
        baseline_yaw: float | None,
        yaw_delta: float | None,
        stability_count: int,
    ) -> dict[str, Any]:
        x, y, width, height = analysis.observation.box
        capture = self._captures.get(session_id)
        response: dict[str, Any] = {
            "challenge_id": session_id,
            "sequence": sequence,
            "current_step": index,
            "total_steps": len(sequence),
            "expected_action": sequence[index] if index < len(sequence) else "COMPLETE",
            "observed_action": action,
            "accepted": accepted,
            "completed": False,
            "feedback": feedback,
            "face_detected": True,
            "quality_score": analysis.quality.score,
            "yaw": analysis.observation.yaw_proxy,
            "eye_aspect_ratio": analysis.eye_aspect_ratio,
            "bounding_box": {
                "x": x / analysis.image_width,
                "y": y / analysis.image_height,
                "width": width / analysis.image_width,
                "height": height / analysis.image_height,
            },
        }
        if self.settings.debug:
            response["debug"] = {
                "expectedPose": expected_pose,
                "detectedPose": action,
                "yawScore": analysis.observation.yaw_proxy,
                "baselineYaw": baseline_yaw,
                "yawDelta": yaw_delta,
                "faceDetected": True,
                "qualityPassed": analysis.quality.accepted,
                "stabilityCount": stability_count,
                "poseAccepted": accepted,
                "analyzedFrames": capture.frame_count if capture else 0,
                "effectiveFps": round(capture.effective_fps() or 0.0, 2) if capture else 0.0,
            }
        return response
