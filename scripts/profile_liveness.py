"""Lightweight synthetic liveness timing report; timings are informational, never CI gates."""

import json
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from app.core.config import Settings
from app.db.database import Database
from app.services.biometrics.embedding import serialize_embedding
from app.services.biometrics.models import FaceObservation, FrameAnalysis, QualityResult
from app.services.challenges import ChallengeService
from app.services.repository import Repository


def analysis(yaw: float = 0.0, ear: float = 0.25) -> FrameAnalysis:
    return FrameAnalysis(
        embedding=np.asarray([1.0, 0.0, 0.0], dtype=np.float32),
        quality=QualityResult(True, 0.95, 100.0, 128.0, "OK"),
        observation=FaceObservation(
            raw=np.zeros(15, dtype=np.float32), confidence=0.99, box=(20, 20, 120, 120), yaw_proxy=yaw
        ),
        eye_aspect_ratio=ear,
        image_width=320,
        image_height=240,
    )


def run_challenge(
    service: ChallengeService, database: Database
) -> tuple[list[float], float, bool]:
    session = service.create("synthetic")
    sequence = json.loads(session["expected_sequence"])
    durations: list[float] = []
    challenge_started = time.perf_counter()
    result: dict[str, object] = {}
    for action in sequence:
        with database.write() as connection:
            connection.execute(
                "UPDATE verification_sessions SET last_capture_at=? WHERE id=?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), session["id"]),
            )
        if action == "CENTER":
            frames = [analysis(), analysis(0.01), analysis(-0.01)]
        elif action == "TURN_LEFT":
            frames = [analysis(0.19), analysis(0.19)]
        elif action == "TURN_RIGHT":
            frames = [analysis(-0.19), analysis(-0.19)]
        else:
            frames = [analysis(ear=ear) for ear in (0.25, 0.25, 0.12, 0.25, 0.25)]
        for frame in frames:
            started = time.perf_counter()
            result = service.analyze(str(session["id"]), frame, b"synthetic")
            durations.append((time.perf_counter() - started) * 1000)
    return durations, (time.perf_counter() - challenge_started) * 1000, bool(result.get("completed"))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="biogate-liveness-") as directory:
        path = Path(directory) / "profile.db"
        settings = Settings(db_path=path, challenge_cooldown_ms=200)
        database = Database(path)
        database.initialize()
        repository = Repository(database)
        repository.create_user("synthetic", "Synthetic profile")
        embedding = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
        repository.upsert_template("synthetic", "test", "1", serialize_embedding(embedding), 3, 1)
        service = ChallengeService(repository, settings)
        run_challenge(service, database)  # Warm caches before collecting informational timings.
        durations: list[float] = []
        completion_times: list[float] = []
        completed = True
        for _ in range(5):
            run_durations, completion_ms, run_completed = run_challenge(service, database)
            durations.extend(run_durations)
            completion_times.append(completion_ms)
            completed = completed and run_completed
        total_ms = sum(completion_times)
        ordered = sorted(durations)
        p95_index = min(len(ordered) - 1, int(len(ordered) * 0.95))
        report = {
            "profile": "synthetic_service_path_without_cv_or_network",
            "runs": len(completion_times),
            "frames": len(durations),
            "median_frame_analysis_ms": round(float(np.median(durations)), 3),
            "p95_frame_analysis_ms": round(ordered[p95_index], 3),
            "effective_analyzed_fps": round(len(durations) * 1000 / total_ms, 2),
            "median_challenge_completion_ms": round(float(np.median(completion_times)), 3),
            "completed": completed,
        }
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
