"""Verification metrics computed from pair scores (cosine similarity) and genuine/impostor labels.

Conventions: a pair is accepted when ``score >= threshold``. FAR is the share of impostor pairs that are
accepted, FRR the share of genuine pairs that are rejected. A pair whose face could not be acquired carries
the score ``-inf`` and is therefore always rejected.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

Scores = NDArray[np.float64]
Labels = NDArray[np.bool_]


@dataclass(frozen=True)
class ThresholdMetrics:
    threshold: float
    far: float
    frr: float


def similarity_distributions(scores: Scores, labels: Labels) -> tuple[Scores, Scores]:
    if scores.shape != labels.shape:
        raise ValueError("Scores and labels must have the same shape")
    return scores[labels], scores[~labels]


def _split(scores: Scores, labels: Labels) -> tuple[Scores, Scores]:
    genuine, impostor = similarity_distributions(scores, labels)
    if genuine.size == 0 or impostor.size == 0:
        raise ValueError("Evaluation requires genuine and impostor pairs")
    return genuine, impostor


def evaluate_threshold(scores: Scores, labels: Labels, threshold: float) -> ThresholdMetrics:
    genuine, impostor = _split(scores, labels)
    return ThresholdMetrics(threshold, float(np.mean(impostor >= threshold)), float(np.mean(genuine < threshold)))


def threshold_grid(start: float = -1.0, stop: float = 1.0, step: float = 0.005) -> list[float]:
    count = int(round((stop - start) / step)) + 1
    return [round(start + index * step, 6) for index in range(count)]


def sweep(scores: Scores, labels: Labels, thresholds: list[float]) -> list[ThresholdMetrics]:
    return [evaluate_threshold(scores, labels, threshold) for threshold in thresholds]


def roc_curve_data(scores: Scores, labels: Labels, points: int = 201) -> list[ThresholdMetrics]:
    if points < 2:
        raise ValueError("At least two ROC points are required")
    return sweep(scores, labels, [float(t) for t in np.linspace(-1.0, 1.0, points)])


def operating_points(scores: Scores, labels: Labels) -> tuple[Scores, Scores, Scores]:
    """Exact FAR/FRR at every distinct finite score used as a threshold (ascending thresholds)."""
    genuine, impostor = _split(scores, labels)
    finite = np.unique(scores[np.isfinite(scores)])
    candidates = np.concatenate([finite, [np.inf]])
    genuine_sorted = np.sort(genuine)
    impostor_sorted = np.sort(impostor)
    far = 1.0 - np.searchsorted(impostor_sorted, candidates, side="left") / impostor.size
    frr = np.searchsorted(genuine_sorted, candidates, side="left") / genuine.size
    return candidates, far, frr


def equal_error_rate(scores: Scores, labels: Labels) -> tuple[float, float]:
    """Return (EER, threshold) at the operating point where FAR and FRR are closest; EER is their mean there."""
    thresholds, far, frr = operating_points(scores, labels)
    index = int(np.argmin(np.abs(far - frr)))
    return float((far[index] + frr[index]) / 2.0), float(thresholds[index])


def threshold_for_far(scores: Scores, labels: Labels, target_far: float) -> ThresholdMetrics:
    """Lowest threshold whose empirical FAR does not exceed ``target_far`` (minimises FRR under the cap)."""
    thresholds, far, frr = operating_points(scores, labels)
    allowed = np.nonzero(far <= target_far)[0]
    index = int(allowed[0])
    return ThresholdMetrics(float(thresholds[index]), float(far[index]), float(frr[index]))


def cross_validated_far_target(
    scores: Scores, labels: Labels, folds: NDArray[np.int64], target_far: float
) -> dict[str, object]:
    """Choose the threshold on all other folds, measure FAR/FRR on the held-out fold."""
    if len(set(int(value) for value in folds)) < 2:
        return {"target_far": target_far, "skipped": "cross-validation needs at least two folds"}
    rows = []
    for fold in sorted(set(int(value) for value in folds)):
        train = folds != fold
        chosen = threshold_for_far(scores[train], labels[train], target_far)
        held_out = evaluate_threshold(scores[~train], labels[~train], chosen.threshold)
        rows.append({"fold": fold, "threshold": chosen.threshold, "far": held_out.far, "frr": held_out.frr})
    far_values = np.asarray([row["far"] for row in rows], dtype=np.float64)
    frr_values = np.asarray([row["frr"] for row in rows], dtype=np.float64)
    return {
        "target_far": target_far,
        "folds": rows,
        "far_mean": float(far_values.mean()),
        "far_std": float(far_values.std(ddof=1)) if far_values.size > 1 else 0.0,
        "frr_mean": float(frr_values.mean()),
        "frr_std": float(frr_values.std(ddof=1)) if frr_values.size > 1 else 0.0,
    }
