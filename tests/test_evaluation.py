import numpy as np
import pytest

from evaluation.lfw import Pair, image_path, parse_pairs
from evaluation.metrics import (
    cross_validated_far_target,
    equal_error_rate,
    evaluate_threshold,
    sweep,
    threshold_for_far,
    threshold_grid,
)
from evaluation.models import Acquisition
from evaluation.plots import det_svg, roc_svg
from evaluation.run import policy_scores

SCORES = np.asarray([0.9, 0.8, 0.7, 0.3, 0.6, 0.4, 0.2, 0.1], dtype=np.float64)
LABELS = np.asarray([True, True, True, True, False, False, False, False])


def test_far_frr_at_threshold_counts_accepts_at_equality() -> None:
    result = evaluate_threshold(SCORES, LABELS, 0.6)
    assert result.far == pytest.approx(0.25)  # 0.6 accepted
    assert result.frr == pytest.approx(0.25)  # 0.3 rejected
    with pytest.raises(ValueError, match="genuine and impostor"):
        evaluate_threshold(SCORES[:4], LABELS[:4], 0.5)


def test_equal_error_rate_and_far_target() -> None:
    eer, threshold = equal_error_rate(SCORES, LABELS)
    assert eer == pytest.approx(0.25)
    assert evaluate_threshold(SCORES, LABELS, threshold).far == pytest.approx(0.25)
    strict = threshold_for_far(SCORES, LABELS, 0.0)
    assert strict.far == 0.0
    assert strict.threshold == pytest.approx(0.7)  # lowest threshold that accepts no impostor
    assert strict.frr == pytest.approx(0.25)


def test_missing_faces_are_always_rejected() -> None:
    scores = np.asarray([0.9, -np.inf, 0.1, -np.inf])
    labels = np.asarray([True, True, False, False])
    result = evaluate_threshold(scores, labels, -1.0)
    assert result.frr == pytest.approx(0.5)
    assert result.far == pytest.approx(0.5)
    assert threshold_for_far(scores, labels, 0.0).frr == pytest.approx(0.5)


def test_cross_validation_chooses_threshold_without_the_held_out_fold() -> None:
    folds = np.asarray([0, 0, 1, 1, 0, 0, 1, 1])
    report = cross_validated_far_target(SCORES, LABELS, folds, 0.0)
    assert [row["fold"] for row in report["folds"]] == [0, 1]  # type: ignore[index]
    assert 0.0 <= float(report["far_mean"]) <= 1.0  # type: ignore[arg-type]
    assert "skipped" in cross_validated_far_target(SCORES, LABELS, np.zeros(8, dtype=np.int64), 0.01)


def test_threshold_grid_and_sweep_are_monotonic() -> None:
    grid = threshold_grid(-1.0, 1.0, 0.5)
    assert grid == [-1.0, -0.5, 0.0, 0.5, 1.0]
    rows = sweep(SCORES, LABELS, grid)
    assert [row.far for row in rows] == sorted((row.far for row in rows), reverse=True)
    assert [row.frr for row in rows] == sorted(row.frr for row in rows)


def test_lfw_pairs_protocol_parsing() -> None:
    text = "2\t1\nA\t1\t2\nA\t1\tB\t3\nC\t2\t4\nC\t1\tD\t1\n"
    pairs = parse_pairs(text)
    assert pairs[0] == Pair(0, "A/A_0001.jpg", "A/A_0002.jpg", True)
    assert pairs[1] == Pair(0, "A/A_0001.jpg", "B/B_0003.jpg", False)
    assert pairs[3].fold == 1
    assert image_path("Abel_Pacheco", 4) == "Abel_Pacheco/Abel_Pacheco_0004.jpg"
    with pytest.raises(ValueError, match="Expected 4 pairs"):
        parse_pairs("2\t1\nA\t1\t2\n")


def test_policies_treat_missing_faces_and_quality_rejections_as_rejects() -> None:
    ok = Acquisition("OK", 1, None, None, None)
    blurry = Acquisition("OK", 1, None, "IMAGE_BLURRY", None)
    missing = Acquisition("NO_FACE", 0, None, None, None)
    acquisitions = {"a": ok, "b": ok, "c": blurry, "d": missing}
    pairs = [Pair(0, "a", "b", True), Pair(0, "a", "c", True), Pair(1, "a", "d", False), Pair(1, "b", "c", False)]
    raw = np.asarray([0.9, 0.8, -np.inf, 0.2])
    scores, labels, folds = policy_scores(pairs, raw, acquisitions, "acquired_pairs")
    assert scores.tolist() == [0.9, 0.8, 0.2] and labels.tolist() == [True, True, False] and folds.tolist() == [0, 0, 1]
    scores, _, _ = policy_scores(pairs, raw, acquisitions, "all_pairs")
    assert scores.tolist() == [0.9, 0.8, -np.inf, 0.2]
    scores, _, _ = policy_scores(pairs, raw, acquisitions, "production_gate")
    assert scores.tolist() == [0.9, -np.inf, -np.inf, -np.inf]


def test_charts_are_standalone_svg() -> None:
    curves = {"model <a>": [(1.0, 0.0), (0.01, 0.02), (0.0, 1.0)]}
    for svg in (det_svg(curves, "DET"), roc_svg(curves, "ROC")):
        assert svg.startswith("<svg") and svg.endswith("</svg>")
        assert "model &lt;a&gt;" in svg
        assert "<polyline" in svg
