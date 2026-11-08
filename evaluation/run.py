"""Reproducible LFW verification benchmark for BioGate embedding models.

    python -m evaluation.lfw                       # download and verify LFW once
    python -m evaluation.run --model sface --model arcface

Writes metrics JSON, threshold sweeps (CSV), per-pair scores and DET/ROC charts to docs/evaluation/.
"""

from __future__ import annotations

import argparse
import csv
import json
import platform
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

from app.core.config import Settings
from app.services.biometrics.model_store import ModelStore
from app.services.biometrics.quality import QualityService
from evaluation.lfw import DEFAULT_ROOT, Pair, parse_pairs, sha256
from evaluation.metrics import (
    cross_validated_far_target,
    equal_error_rate,
    evaluate_threshold,
    operating_points,
    sweep,
    threshold_for_far,
    threshold_grid,
)
from evaluation.models import ARCFACE_R100, Acquirer, ArcFaceEmbedder, Embedder, SFaceEmbedder, timed_embed
from evaluation.plots import det_svg, roc_svg

TARGET_FARS = (0.01, 0.001)
POLICIES = {
    "acquired_pairs": "only pairs where YuNet found a face in both images",
    "all_pairs": "every pair; a missing face counts as a reject (score -inf)",
    "production_gate": "every pair; a missing face or an image failing the enrollment quality gate counts as a reject",
}


def build_embedder(key: str, store: ModelStore, arcface_model: Path | None) -> Embedder:
    if key == "sface":
        return SFaceEmbedder(store)
    if key == "arcface":
        return ArcFaceEmbedder(arcface_model or store.ensure(ARCFACE_R100))
    raise ValueError(f"Unknown model {key}")


def percentile(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), q))


def policy_scores(
    pairs: list[Pair], raw: NDArray[np.float64], acquisitions: dict[str, Any], policy: str
) -> tuple[NDArray[np.float64], NDArray[np.bool_], NDArray[np.int64]]:
    labels = np.asarray([pair.genuine for pair in pairs], dtype=np.bool_)
    folds = np.asarray([pair.fold for pair in pairs], dtype=np.int64)
    acquired = np.asarray(
        [acquisitions[p.first].status == "OK" and acquisitions[p.second].status == "OK" for p in pairs], dtype=np.bool_
    )
    if policy == "acquired_pairs":
        return raw[acquired], labels[acquired], folds[acquired]
    scores = raw.copy()
    if policy == "production_gate":
        gated = np.asarray(
            [
                acquisitions[p.first].quality_reason is None and acquisitions[p.second].quality_reason is None
                for p in pairs
            ],
            dtype=np.bool_,
        )
        scores[~gated] = -np.inf
    scores[~acquired] = -np.inf
    return scores, labels, folds


def summarize(
    scores: NDArray[np.float64], labels: NDArray[np.bool_], folds: NDArray[np.int64], production: float
) -> dict[str, Any]:
    eer, eer_threshold = equal_error_rate(scores, labels)
    at_production = evaluate_threshold(scores, labels, production)
    targets = {}
    for target in TARGET_FARS:
        chosen = threshold_for_far(scores, labels, target)
        targets[f"far_{target:g}"] = {
            "threshold_on_all_pairs": chosen.threshold,
            "far_on_all_pairs": chosen.far,
            "frr_on_all_pairs": chosen.frr,
            "cross_validated": cross_validated_far_target(scores, labels, folds, target),
        }
    return {
        "pairs": int(labels.size),
        "genuine_pairs": int(labels.sum()),
        "impostor_pairs": int((~labels).sum()),
        "eer": eer,
        "eer_threshold": eer_threshold,
        "production_threshold": {"threshold": production, "far": at_production.far, "frr": at_production.frr},
        "far_targets": targets,
    }


def write_charts(output_dir: Path, model_scores: dict[str, NDArray[np.float64]], labels: NDArray[np.bool_]) -> None:
    """DET/ROC over exact operating points of pairs where both faces were found."""
    curves: dict[str, list[tuple[float, float]]] = {}
    for name, raw in model_scores.items():
        found = np.isfinite(raw)
        _, far, frr = operating_points(raw[found], labels[found])
        curves[name] = list(zip(far.tolist(), frr.tolist(), strict=True))
    (output_dir / "det.svg").write_text(det_svg(curves, "LFW View 2, DET (pairs with both faces found)"), "utf-8")
    (output_dir / "roc.svg").write_text(roc_svg(curves, "LFW View 2, ROC (pairs with both faces found)"), "utf-8")


def replot(output_dir: Path) -> None:
    report = json.loads((output_dir / "metrics.json").read_text(encoding="utf-8"))
    with (output_dir / "pair_scores.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    labels = np.asarray([row["genuine"] == "1" for row in rows], dtype=np.bool_)
    scores = {
        report["models"][key]["model"]: np.asarray([float(row[f"{key}_score"]) for row in rows], dtype=np.float64)
        for key in report["models"]
    }
    write_charts(output_dir, scores, labels)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--pairs-file", type=Path, help="LFW-format pairs file (default: <dataset-root>/pairs.txt)")
    parser.add_argument("--images-dir", type=Path, help="image root (default: <dataset-root>/lfw)")
    parser.add_argument("--model", action="append", choices=["sface", "arcface"], default=[])
    parser.add_argument("--arcface-model", type=Path, help="existing arcfaceresnet100-8.onnx (else downloaded)")
    parser.add_argument("--model-cache", type=Path, default=Path("model_cache"))
    parser.add_argument("--output-dir", type=Path, default=Path("docs/evaluation"))
    parser.add_argument("--limit-pairs", type=int, default=0, help="smoke runs only; 0 = full protocol")
    parser.add_argument("--replot", action="store_true", help="redraw charts from existing pair_scores.csv only")
    args = parser.parse_args()
    if args.replot:
        replot(args.output_dir)
        return 0
    models = args.model or ["sface"]

    settings = Settings(_env_file=None, model_cache=args.model_cache)  # defaults only, never the local .env
    production = settings.identification_threshold
    store = ModelStore(args.model_cache)
    pairs_file = args.pairs_file or args.dataset_root / "pairs.txt"
    pairs = parse_pairs(pairs_file.read_text(encoding="utf-8"))
    if args.limit_pairs:
        pairs = pairs[: args.limit_pairs]
    images_root = args.images_dir or args.dataset_root / "lfw"
    unique = sorted({path for pair in pairs for path in (pair.first, pair.second)})

    quality = QualityService(
        settings.min_face_pixels, settings.min_sharpness, settings.min_brightness, settings.max_brightness
    )
    acquirer = Acquirer(store, quality)
    acquisitions = {path: acquirer.acquire(images_root / path) for path in unique}
    statuses = Counter(item.status for item in acquisitions.values())
    gate = Counter(item.quality_reason for item in acquisitions.values() if item.status == "OK" and item.quality_reason)
    acquisition_report = {
        "images": len(unique),
        "status": dict(statuses),
        "multiple_faces_central_face_used": sum(1 for item in acquisitions.values() if item.face_count > 1),
        "enrollment_quality_gate_rejections": dict(gate),
        "pairs_with_missing_face": sum(
            1 for p in pairs if acquisitions[p.first].status != "OK" or acquisitions[p.second].status != "OK"
        ),
        "genuine_pairs_with_missing_face": sum(
            1
            for p in pairs
            if p.genuine and (acquisitions[p.first].status != "OK" or acquisitions[p.second].status != "OK")
        ),
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    pair_scores: dict[str, NDArray[np.float64]] = {}
    results: dict[str, Any] = {}
    for key in models:
        embedder = build_embedder(key, store, args.arcface_model)
        embeddings: dict[str, NDArray[np.float32]] = {}
        timings: list[float] = []
        for path, item in acquisitions.items():
            if item.aligned is not None:
                embeddings[path], elapsed = timed_embed(embedder, item.aligned)
                timings.append(elapsed)
        raw = np.asarray(
            [
                float(np.dot(embeddings[p.first], embeddings[p.second]))
                if p.first in embeddings and p.second in embeddings
                else -np.inf
                for p in pairs
            ],
            dtype=np.float64,
        )
        pair_scores[key] = raw
        policies = {name: summarize(*policy_scores(pairs, raw, acquisitions, name), production) for name in POLICIES}
        results[key] = {
            "model": embedder.name,
            "model_file": embedder.model_file.name,
            "model_bytes": embedder.model_file.stat().st_size,
            "model_sha256": sha256(embedder.model_file),
            "embedding_ms": {
                "count": len(timings),
                "median": percentile(timings, 50),
                "p95": percentile(timings, 95),
                "note": "alignment excluded; one aligned 112x112 crop -> normalised embedding, single thread CPU",
            },
            "policies": policies,
        }
        scores, labels, _ = policy_scores(pairs, raw, acquisitions, "acquired_pairs")
        with (args.output_dir / f"{key}_thresholds.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["threshold", "far_acquired_pairs", "frr_acquired_pairs", "far_all_pairs", "frr_all_pairs"])
            all_scores, all_labels, _ = policy_scores(pairs, raw, acquisitions, "all_pairs")
            grid = threshold_grid()
            for acquired_row, all_row in zip(
                sweep(scores, labels, grid), sweep(all_scores, all_labels, grid), strict=True
            ):
                writer.writerow([acquired_row.threshold, acquired_row.far, acquired_row.frr, all_row.far, all_row.frr])

    with (args.output_dir / "pair_scores.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["pair_index", "fold", "genuine", *[f"{key}_score" for key in pair_scores]])
        for index, pair in enumerate(pairs):
            writer.writerow(
                [index, pair.fold, int(pair.genuine), *[f"{pair_scores[key][index]:.6f}" for key in pair_scores]]
            )

    write_charts(
        args.output_dir,
        {results[key]["model"]: raw for key, raw in pair_scores.items()},
        np.asarray([pair.genuine for pair in pairs], dtype=np.bool_),
    )

    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "protocol": {
            "dataset": "LFW, original (unaligned) images, View 2 pairs.txt",
            "pairs_file_sha256": sha256(pairs_file),
            "pairs": len(pairs),
            "folds": len({pair.fold for pair in pairs}),
            "limited_smoke_run": bool(args.limit_pairs),
            "face_selection": "YuNet (production settings); with several faces the one closest to the image centre",
            "alignment": "OpenCV FaceRecognizerSF.alignCrop, 112x112 five-point template",
            "score": "cosine similarity of L2-normalised embeddings; accept when score >= threshold",
            "threshold_selection": "lowest threshold with FAR <= target; cross-validated over the 10 folds",
            "policies": POLICIES,
            "seed": "none needed: pairs, folds and processing are deterministic",
        },
        "environment": {
            "python": sys.version.split()[0],
            "opencv": cv2.__version__,
            "numpy": np.__version__,
            "platform": platform.platform(),
            "processor": platform.processor(),
        },
        "acquisition": acquisition_report,
        "models": results,
    }
    (args.output_dir / "metrics.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", "utf-8")
    print(
        json.dumps(
            {"acquisition": acquisition_report, **{k: v["policies"]["acquired_pairs"] for k, v in results.items()}},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
