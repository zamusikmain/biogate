import hashlib
import hmac
import secrets
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from numpy.typing import NDArray

from app.core.config import Settings
from app.services.biometrics.embedding import aggregate
from app.services.biometrics.models import FrameAnalysis
from app.services.challenges import BlinkStateMachine, ChallengeService, EphemeralCapture
from app.services.identification import IdentificationService
from app.services.repository import Repository


@dataclass
class FaceLoginState:
    id: str
    mode: str
    sequence: list[str]
    expires_at: datetime
    target_user_id: int | None = None
    index: int = 0
    blink_phase: str = "WAITING_OPEN"
    last_capture_at: datetime | None = None
    capture: EphemeralCapture = field(default_factory=EphemeralCapture)


class FaceLoginService:
    """Server-owned liveness for 1:N login and targeted recovery."""

    def __init__(
        self,
        repository: Repository,
        settings: Settings,
        challenge_service: ChallengeService,
        identification: IdentificationService,
    ):
        self.repository = repository
        self.settings = settings
        self.challenge_service = challenge_service
        self.identification = identification
        self.blink = BlinkStateMachine()
        self._states: dict[str, FaceLoginState] = {}
        self._recovery_starts: dict[str, list[datetime]] = {}
        self._lock = threading.RLock()

    def create(self, mode: str = "IDENTIFY", login: str | None = None, client_address: str = "") -> dict[str, Any]:
        if mode == "IDENTIFY" and not self.settings.allow_face_login:
            raise ValueError("FACE_LOGIN_DISABLED")
        target_user_id = None
        if mode == "RECOVERY" and login:
            self._record_recovery_start(login, client_address)
            user = self.repository.get_user_by_login(login)
            if (
                user
                and user["status"] == "ACTIVE"
                and bool(user.get("face_enabled", 1))
                and bool(user.get("biometric_recovery_enabled", 1))
                and self.repository.get_template(int(user["id"]))
            ):
                target_user_id = int(user["id"])
        tail = ["TURN_LEFT", "TURN_RIGHT", "BLINK"]
        secrets.SystemRandom().shuffle(tail)
        now = datetime.now(UTC)
        state = FaceLoginState(
            id=str(uuid.uuid4()),
            mode=mode,
            sequence=["CENTER", *tail],
            expires_at=now + timedelta(seconds=self.settings.challenge_ttl_seconds),
            target_user_id=target_user_id,
        )
        with self._lock:
            self._states[state.id] = state
        return self._public_state(state)

    def expected_action(self, challenge_id: str) -> str:
        with self._lock:
            state = self._states.get(challenge_id)
            if not state:
                raise KeyError("CHALLENGE_NOT_FOUND")
            return state.sequence[state.index] if state.index < len(state.sequence) else "COMPLETE"

    def analyze(
        self,
        challenge_id: str,
        analysis: FrameAnalysis,
        frame_data: bytes,
        embedding_factory: Callable[[], NDArray[np.float32]] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            state = self._states.get(challenge_id)
            if not state:
                raise KeyError("CHALLENGE_NOT_FOUND")
            if datetime.now(UTC) >= state.expires_at:
                self._states.pop(challenge_id, None)
                raise TimeoutError("CHALLENGE_TIMEOUT")
            action = state.sequence[state.index]
            state.capture.record_frame()
            accepted = False
            detected = "BLINK"
            yaw_delta = (
                analysis.observation.yaw_proxy - state.capture.baseline_yaw
                if state.capture.baseline_yaw is not None
                else None
            )
            now = datetime.now(UTC)
            if action == "BLINK":
                centered = (
                    state.capture.baseline_yaw is not None
                    and abs(analysis.observation.yaw_proxy - state.capture.baseline_yaw)
                    <= self.settings.head_center_dead_zone
                )
                state.blink_phase, blink_complete = self.blink.advance(
                    state.capture.blink,
                    analysis.eye_aspect_ratio,
                    analysis.left_eye_aspect_ratio,
                    analysis.right_eye_aspect_ratio,
                    centered=centered,
                )
                accepted = blink_complete and self._cooldown_elapsed(state, now)
                feedback = f"BLINK_{state.blink_phase}"
            else:
                pose = self.challenge_service._evaluate_pose(state.capture, action, analysis.observation.yaw_proxy)
                detected, yaw_delta = pose.detected_pose, pose.yaw_delta
                if state.capture.stability_step != state.index:
                    state.capture.stability_step, state.capture.stability_count = state.index, 0
                state.capture.stability_count = state.capture.stability_count + 1 if pose.matches else 0
                required = 1 if action == "CENTER" else self.settings.head_stability_frames
                accepted = state.capture.stability_count >= required and self._cooldown_elapsed(state, now)
                feedback = "STABLE" if pose.matches else "ADJUST_POSE"
            if accepted:
                state.last_capture_at = now
                state.capture.embeddings.append(embedding_factory() if embedding_factory else analysis.embedding)
                state.capture.quality_scores.append(analysis.quality.score)
                if state.capture.snapshot is None and action == "CENTER":
                    state.capture.snapshot = frame_data
                state.index += 1
                if state.index >= len(state.sequence):
                    return self._finalize(state)
            result = self._public_state(state)
            guidance_reason = None
            if action == "CENTER" and not accepted:
                if abs(analysis.observation.yaw_proxy) > self.settings.head_max_center_yaw:
                    guidance_reason = "POSE_NOT_CENTERED"
                elif (
                    len(state.capture.baseline_samples) >= self.settings.head_baseline_frames
                    and max(state.capture.baseline_samples) - min(state.capture.baseline_samples)
                    > self.settings.head_calibration_range
                ):
                    guidance_reason = "UNSTABLE"
            elif action == "TURN_LEFT" and detected == "BETWEEN_POSES":
                guidance_reason = "TURN_MORE_LEFT"
            elif action == "TURN_RIGHT" and detected == "BETWEEN_POSES":
                guidance_reason = "TURN_MORE_RIGHT"
            elif action == "BLINK" and (
                state.capture.baseline_yaw is None
                or abs(analysis.observation.yaw_proxy - state.capture.baseline_yaw)
                > self.settings.head_center_dead_zone
            ):
                guidance_reason = "BLINK_CENTER"
            result.update(
                {
                    "observed_action": detected,
                    "accepted": accepted,
                    "completed": False,
                    "feedback": feedback,
                    "guidance_reason": guidance_reason,
                    "quality_score": analysis.quality.score,
                    "face_geometry": {
                        "x": analysis.observation.box[0],
                        "y": analysis.observation.box[1],
                        "width": analysis.observation.box[2],
                        "height": analysis.observation.box[3],
                        "image_width": analysis.image_width,
                        "image_height": analysis.image_height,
                    },
                }
            )
            if self.settings.debug:
                result["debug"] = {
                    "expectedPose": action,
                    "detectedPose": detected,
                    "baselineYaw": state.capture.baseline_yaw,
                    "yawDelta": yaw_delta,
                    "stabilityCount": state.capture.stability_count,
                    "blinkPhase": state.blink_phase,
                    "analyzedFrames": state.capture.frame_count,
                    "effectiveFps": round(state.capture.effective_fps() or 0.0, 2),
                }
            return result

    def _finalize(self, state: FaceLoginState) -> dict[str, Any]:
        probe = aggregate(state.capture.embeddings)
        if state.mode == "RECOVERY":
            result = self._targeted_recovery(state, probe)
        else:
            identification = self.identification.identify(probe)
            result = {
                "decision": identification.decision,
                "best_similarity": identification.best.similarity if identification.best else None,
                "second_similarity": identification.second.similarity if identification.second else None,
                "threshold": identification.threshold,
                "margin": identification.margin,
                "_user_id": (
                    identification.best.user_id
                    if identification.best and identification.decision in {"IDENTIFIED", "BLOCKED", "DISABLED"}
                    else None
                ),
                "_security_metadata": {
                    "best_user_id": identification.best.user_id if identification.best else None,
                    "second_user_id": identification.second.user_id if identification.second else None,
                    "best_similarity": identification.best.similarity if identification.best else None,
                    "second_similarity": identification.second.similarity if identification.second else None,
                    "margin": identification.margin,
                },
            }
            if identification.decision == "IDENTIFIED":
                result["account"] = self._candidate(identification.best)
        result.update(
            {
                "challenge_id": state.id,
                "completed": True,
                "liveness": True,
                "_evidence": state.capture.snapshot,
            }
        )
        self._states.pop(state.id, None)
        return result

    def _targeted_recovery(self, state: FaceLoginState, probe: NDArray[np.float32]) -> dict[str, Any]:
        if state.target_user_id is None:
            return {"decision": "DENIED", "similarity": None, "threshold": self.settings.verification_threshold}
        template = self.repository.get_template(state.target_user_id)
        if not template:
            return {"decision": "DENIED", "similarity": None, "threshold": self.settings.verification_threshold}
        similarity = self.identification.identity_score(probe, template)
        return {
            "decision": "RECOVERY_VERIFIED" if similarity >= self.settings.verification_threshold else "DENIED",
            "similarity": similarity,
            "threshold": self.settings.verification_threshold,
            "_user_id": state.target_user_id,
        }

    def _cooldown_elapsed(self, state: FaceLoginState, now: datetime) -> bool:
        if state.last_capture_at is None:
            return True
        elapsed_ms = (now - state.last_capture_at).total_seconds() * 1000
        return elapsed_ms >= self.settings.challenge_cooldown_ms

    def _record_recovery_start(self, login: str, client_address: str) -> None:
        secret = self.settings.session_secret.get_secret_value()
        material = f"{client_address}|{login.casefold()}"
        key = hmac.new(secret.encode(), material.encode(), hashlib.sha256).hexdigest()
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=self.settings.recovery_window_seconds)
        with self._lock:
            starts = [item for item in self._recovery_starts.get(key, []) if item >= cutoff]
            if len(starts) >= self.settings.recovery_max_attempts:
                raise PermissionError("RECOVERY_RATE_LIMITED")
            starts.append(now)
            self._recovery_starts[key] = starts

    @staticmethod
    def _candidate(candidate: Any) -> dict[str, Any] | None:
        if candidate is None:
            return None
        return {
            "external_id": candidate.external_id,
            "full_name": candidate.full_name,
            "similarity": candidate.similarity,
        }

    @staticmethod
    def _public_state(state: FaceLoginState) -> dict[str, Any]:
        return {
            "challenge_id": state.id,
            "sequence": state.sequence,
            "current_step": state.index,
            "total_steps": len(state.sequence),
            "expected_action": state.sequence[state.index] if state.index < len(state.sequence) else "COMPLETE",
            "expires_at": state.expires_at.isoformat(),
        }
