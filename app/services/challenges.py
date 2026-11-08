"""Liveness building blocks for face login: blink detection and head-pose evaluation.

The V2 1:1 verification flow that used to live here was removed; FaceLoginService
owns the challenge state now. Historical V2 attempts stay readable in the database.
"""

import statistics
import time
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from app.core.config import Settings


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


def evaluate_pose(capture: EphemeralCapture, action: str, yaw: float, settings: Settings) -> PoseEvaluation:
    """Calibrate the CENTER baseline, then classify later frames relative to it."""
    if action == "CENTER" and capture.baseline_yaw is None:
        capture.baseline_samples.append(yaw)
        capture.baseline_samples = capture.baseline_samples[-settings.head_baseline_frames :]
        stable = (
            len(capture.baseline_samples) >= settings.head_baseline_frames
            and max(capture.baseline_samples) - min(capture.baseline_samples) <= settings.head_calibration_range
            and abs(statistics.median(capture.baseline_samples)) <= settings.head_max_center_yaw
        )
        if stable:
            capture.baseline_yaw = float(statistics.median(capture.baseline_samples))
            return PoseEvaluation("CENTER", 0.0, True)
        return PoseEvaluation("CALIBRATING", None, False)

    if capture.baseline_yaw is None:
        return PoseEvaluation("UNCALIBRATED", None, False)
    delta = yaw - capture.baseline_yaw
    if abs(delta) <= settings.head_center_dead_zone:
        detected = "CENTER"
    # YuNet's yaw proxy stays in the original, unmirrored image coordinates:
    # image-right is the user's physical left for a front-facing camera.
    # Keep that diagnostic sign, but map it to the user's physical direction.
    elif delta >= settings.head_turn_delta:
        detected = "TURN_LEFT"
    elif delta <= -settings.head_turn_delta:
        detected = "TURN_RIGHT"
    else:
        detected = "BETWEEN_POSES"
    return PoseEvaluation(detected, delta, detected == action)
