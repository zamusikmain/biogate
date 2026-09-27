import argparse
import csv
import json
import time
from pathlib import Path

import numpy as np

from app.core.config import get_settings
from app.services.biometrics.embedding import cosine_similarity
from app.services.biometrics.pipeline import BiometricPipeline
from evaluation.metrics import evaluate_threshold, roc_curve_data, similarity_distributions


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate BioGate on an explicit pair manifest")
    parser.add_argument("manifest", type=Path, help="CSV with image_a,image_b,is_genuine")
    parser.add_argument("--output", type=Path, default=Path("evaluation/output/metrics.json"))
    parser.add_argument("--threshold", type=float, default=None)
    args = parser.parse_args()
    settings = get_settings()
    threshold = settings.verification_threshold if args.threshold is None else args.threshold
    pipeline = BiometricPipeline(settings)
    scores: list[float] = []
    labels: list[bool] = []
    latencies: list[float] = []
    base = args.manifest.parent
    with args.manifest.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            started = time.perf_counter()
            left = pipeline.process_frame((base / row["image_a"]).read_bytes()).embedding
            right = pipeline.process_frame((base / row["image_b"]).read_bytes()).embedding
            scores.append(cosine_similarity(left, right))
            labels.append(row["is_genuine"].strip().lower() in {"1", "true", "yes"})
            latencies.append((time.perf_counter() - started) * 1000)
    score_array = np.asarray(scores, dtype=np.float64)
    label_array = np.asarray(labels, dtype=np.bool_)
    genuine, impostor = similarity_distributions(score_array, label_array)
    metrics = evaluate_threshold(score_array, label_array, threshold)
    output = {
        "pair_count": len(scores),
        "genuine_count": int(genuine.size),
        "impostor_count": int(impostor.size),
        "threshold": threshold,
        "far": metrics.far,
        "frr": metrics.frr,
        "mean_pair_latency_ms": float(np.mean(latencies)),
        "genuine_scores": genuine.tolist(),
        "impostor_scores": impostor.tolist(),
        "roc": [item.__dict__ for item in roc_curve_data(score_array, label_array)],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {key: output[key] for key in ("pair_count", "threshold", "far", "frr", "mean_pair_latency_ms")},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
