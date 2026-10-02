"""Offline biometric evaluation for an explicitly supplied, consented test dataset."""

from __future__ import annotations

import argparse
import csv
import json
from itertools import combinations
from pathlib import Path
from statistics import fmean
from typing import Any

import numpy as np
from numpy.typing import NDArray

from app.core.config import Settings
from app.services.biometrics.embedding import aggregate, cosine_similarity
from app.services.biometrics.pipeline import BiometricPipeline

PRODUCTION_THRESHOLD = 0.363
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def evaluate_embeddings(identities: dict[str, list[NDArray[np.float32]]], thresholds: list[float]) -> dict[str, Any]:
    if not identities or not any(identities.values()):
        raise ValueError("EMPTY_DATASET")
    genuine = [
        cosine_similarity(left, right) for samples in identities.values() for left, right in combinations(samples, 2)
    ]
    impostor = [
        cosine_similarity(left, right)
        for first, second in combinations(sorted(identities), 2)
        for left in identities[first]
        for right in identities[second]
    ]
    threshold_rows = []
    for threshold in sorted(set([*thresholds, PRODUCTION_THRESHOLD])):
        false_accepts = sum(score >= threshold for score in impostor)
        false_rejects = sum(score < threshold for score in genuine)
        threshold_rows.append(
            {
                "threshold": threshold,
                "production_threshold": threshold == PRODUCTION_THRESHOLD,
                "false_accepts": false_accepts,
                "false_accept_rate": false_accepts / len(impostor) if impostor else None,
                "false_rejects": false_rejects,
                "false_reject_rate": false_rejects / len(genuine) if genuine else None,
            }
        )
    centroids = {identity: aggregate(samples) for identity, samples in identities.items() if samples}
    margins: list[float] = []
    if len(centroids) >= 2:
        for identity, samples in identities.items():
            for sample in samples:
                scores = sorted(
                    (cosine_similarity(sample, centroid), candidate) for candidate, centroid in centroids.items()
                )
                scores.reverse()
                if scores[0][1] == identity:
                    margins.append(scores[0][0] - scores[1][0])
    return {
        "identities": len(identities),
        "images": sum(len(samples) for samples in identities.values()),
        "genuine_comparisons": len(genuine),
        "impostor_comparisons": len(impostor),
        "genuine_similarity": distribution(genuine),
        "impostor_similarity": distribution(impostor),
        "top1_top2_margin": distribution(margins),
        "thresholds": threshold_rows,
    }


def distribution(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "mean": fmean(values) if values else None,
    }


def load_dataset(dataset: Path, pipeline: BiometricPipeline) -> dict[str, list[NDArray[np.float32]]]:
    if not dataset.is_dir():
        raise ValueError("DATASET_NOT_FOUND")
    identities: dict[str, list[NDArray[np.float32]]] = {}
    for identity_dir in sorted(path for path in dataset.iterdir() if path.is_dir()):
        images = sorted(
            path for path in identity_dir.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        )
        if images:
            identities[identity_dir.name] = [pipeline.process_frame(path.read_bytes()).embedding for path in images]
    if not identities:
        raise ValueError("EMPTY_DATASET")
    return identities


def write_csv(path: Path, report: dict[str, Any]) -> None:
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(report["thresholds"][0]))
        writer.writeheader()
        writer.writerows(report["thresholds"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Local offline evaluation on an explicitly supplied test dataset")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--threshold", type=float, action="append", default=[])
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-csv", type=Path)
    parser.add_argument("--model-cache", type=Path, default=Path("model_cache"))
    args = parser.parse_args()
    settings = Settings(_env_file=None, model_cache=args.model_cache)
    pipeline = BiometricPipeline(settings)
    report = evaluate_embeddings(load_dataset(args.dataset.resolve(), pipeline), args.threshold)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output_json:
        args.output_json.write_text(rendered + "\n", encoding="utf-8")
    if args.output_csv:
        write_csv(args.output_csv, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
