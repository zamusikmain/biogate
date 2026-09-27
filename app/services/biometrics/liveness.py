from app.services.biometrics.models import FaceObservation, LivenessResult


class LivenessService:
    """Active head-turn challenge; useful against static photos, not full PAD."""

    def __init__(self, minimum_yaw_range: float = 0.14):
        self.minimum_yaw_range = minimum_yaw_range

    def evaluate(self, observations: list[FaceObservation]) -> LivenessResult:
        if len(observations) < 3:
            return LivenessResult(False, 0.0, "LIVENESS_INSUFFICIENT_FRAMES", 0.0)
        yaws = [item.yaw_proxy for item in observations]
        observed_range = max(yaws) - min(yaws)
        score = min(observed_range / max(self.minimum_yaw_range * 2, 1e-6), 1.0)
        passed = observed_range >= self.minimum_yaw_range
        return LivenessResult(passed, round(score, 4), "OK" if passed else "LIVENESS_FAILED", observed_range)
