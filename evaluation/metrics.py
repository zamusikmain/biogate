from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class ThresholdMetrics:
    threshold: float
    far: float
    frr: float


def similarity_distributions(
    scores: NDArray[np.float64], labels: NDArray[np.bool_]
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    if scores.shape != labels.shape:
        raise ValueError("Scores and labels must have the same shape")
    return scores[labels], scores[~labels]


def evaluate_threshold(scores: NDArray[np.float64], labels: NDArray[np.bool_], threshold: float) -> ThresholdMetrics:
    genuine, impostor = similarity_distributions(scores, labels)
    if genuine.size == 0 or impostor.size == 0:
        raise ValueError("Evaluation requires genuine and impostor pairs")
    far = float(np.mean(impostor >= threshold))
    frr = float(np.mean(genuine < threshold))
    return ThresholdMetrics(threshold, far, frr)


def roc_curve_data(scores: NDArray[np.float64], labels: NDArray[np.bool_], points: int = 201) -> list[ThresholdMetrics]:
    if points < 2:
        raise ValueError("At least two ROC points are required")
    return [evaluate_threshold(scores, labels, float(t)) for t in np.linspace(-1.0, 1.0, points)]
