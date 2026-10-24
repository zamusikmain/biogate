# Evaluation tool

One tool measures BioGate's face verification accuracy on the public LFW pairs protocol.
Protocol, licences, results and limitations: [docs/EVALUATION.md](../docs/EVALUATION.md).

```powershell
.\.venv\Scripts\python.exe -m evaluation.lfw                     # download + SHA-256 check + extract (once)
.\.venv\Scripts\python.exe -m evaluation.run --model sface        # production model
.\.venv\Scripts\python.exe -m evaluation.run --model sface --model arcface   # with the comparison model
```

- `lfw.py` parses `pairs.txt` (View 2, 10 folds x 600 pairs) and downloads the dataset into
  `evaluation/datasets/` (ignored by Git; images are never committed).
- `models.py` acquires faces with the production YuNet detector and alignment, and embeds them with SFace
  or the comparison ArcFace model.
- `metrics.py` computes FAR/FRR, EER, thresholds for a target FAR and 10-fold cross-validation.
- `plots.py` writes DET and ROC charts as SVG without extra dependencies.
- `run.py` writes `metrics.json`, `<model>_thresholds.csv`, `pair_scores.csv`, `det.svg` and `roc.svg`
  to `docs/evaluation/`.

The tool never reads the application database, evidence or `.env`, and never changes the production threshold.
