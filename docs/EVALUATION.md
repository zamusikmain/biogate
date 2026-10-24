# Accuracy evaluation

All numbers below come from one run of `python -m evaluation.run --model sface --model arcface` on
2026-10-24 (raw output: [`docs/evaluation/metrics.json`](evaluation/metrics.json)). Nothing is rounded in
the project's favour; percentages are exact counts divided by the number of pairs.

## Protocol

| Item | Value |
|---|---|
| Dataset | Labeled Faces in the Wild (LFW), original unaligned 250x250 JPEGs, 13,233 images of 5,749 people |
| Pairs | LFW **View 2** `pairs.txt`: 10 folds x (300 genuine + 300 impostor) = **6,000 pairs** (3,000 / 3,000), 7,701 distinct images |
| Source | scikit-learn figshare mirror (`ndownloader.figshare.com/files/5976018` and `/5976006`); the original UMass site did not resolve from this machine |
| `lfw.tgz` SHA-256 | `055f7d9c632d7370e6fb4afc7468d40f970c34a80d4c6f50ffec63f5a8d536c0` (180,566,744 bytes) |
| `pairs.txt` SHA-256 | `ea42330c62c92989f9d7c03237ed5d591365e89b3e649747777b70e692dc1592` |
| Face finding | production YuNet detector (`face_detection_yunet_2023mar.onnx`, SHA-256 `8f2383e4…2552fa4`), confidence 0.85; if several faces are found, the one closest to the image centre is used |
| Alignment | production `FaceRecognizerSF.alignCrop`, 112x112, five landmarks |
| Score | cosine similarity of L2-normalised embeddings; a pair is accepted when `score >= threshold` |
| Threshold for a target FAR | lowest threshold whose FAR does not exceed the target; also 10-fold cross-validated (choose on 9 folds, measure on the held-out fold) |
| Seed | none needed: pairs, folds and processing are deterministic |
| Environment | Python 3.12.14, OpenCV 4.10.0, NumPy 2.2.5, Windows 11, Intel CPU (Family 6 Model 181), single process |

Images are never committed (`evaluation/datasets/` is ignored by Git). Reproduce:

```powershell
.\.venv\Scripts\python.exe -m evaluation.lfw
.\.venv\Scripts\python.exe -m evaluation.run --model sface --model arcface
.\.venv\Scripts\python.exe -m evaluation.run --replot      # charts only, from pair_scores.csv
```

The full run took 1,821 s on the machine above.

## Face acquisition on LFW

| | Images |
|---|---|
| Face found | 7,700 of 7,701 |
| No face found by YuNet | **1** (it belongs to one genuine pair) |
| Several faces, central face used | 973 |
| Would fail the production quality gate | 524 (298 `IMAGE_BLURRY`, 226 `FACE_TOO_SMALL`) |

Effect of the missing face: one genuine pair cannot be scored. Counted as a reject it adds one false
reject (SFace FRR at 0.363 goes from 46/2,999 = 1.534 % to 47/3,000 = 1.567 %).

The production application would also refuse the 973 images with several faces (`MULTIPLE_FACES`); the
benchmark keeps them by choosing the central face, which is the LFW convention.

## Results

Policies: **acquired** = 5,999 pairs where both faces were found; **all** = 6,000 pairs, a missing face is a
reject; **gate** = 6,000 pairs, a missing face or an image failing the enrollment quality gate is a reject.

### SFace (production model)

| Metric | acquired | all | gate |
|---|---|---|---|
| EER | 0.800 % (thr 0.3000) | 0.833 % (thr 0.2987) | 12.43 % |
| At **0.363**: FAR | 2/3,000 = 0.067 % | 0.067 % | 1/3,000 = 0.033 % |
| At **0.363**: FRR | 46/2,999 = 1.534 % | 47/3,000 = 1.567 % | 403/3,000 = 13.43 % |
| FAR <= 1 %: threshold | 0.2949 | 0.2949 | 0.2897 |
| FAR <= 1 %: FAR / FRR | 0.967 % / 0.767 % | 0.967 % / 0.800 % | 0.967 % / 12.80 % |
| FAR <= 1 %, 10-fold CV: FAR / FRR | 0.933 ± 0.644 % / 0.767 ± 0.687 % | 0.933 % / 0.800 ± 0.740 % | 1.000 % / 12.80 % |
| FAR <= 0.1 %: threshold | 0.3641 | 0.3641 | 0.3367 |
| FAR <= 0.1 %: FAR / FRR | 0.067 % / 1.534 % | 0.067 % / 1.567 % | 0.067 % / 13.10 % |
| FAR <= 0.1 %, 10-fold CV: FAR / FRR | 0.100 ± 0.225 % / 1.468 ± 1.158 % | 0.100 % / 1.500 ± 1.210 % | 0.100 % / 13.10 % |

Cross-validated thresholds per fold: 0.2918–0.2985 for FAR <= 1 %, 0.3367–0.3648 for FAR <= 0.1 %.

### Model comparison (policy "acquired", same pairs, same detector and alignment)

| Model | EER | FRR @ FAR <= 1 % | FRR @ FAR <= 0.1 % | Embedding time, median / p95 | Model size |
|---|---|---|---|---|---|
| **SFace** (OpenCV Zoo 2021dec, production) | 0.800 % | 0.767 % (23/2,999) | 1.534 % (46/2,999) | 16.7 / 20.3 ms | 38,696,353 B (36.9 MiB) |
| **ArcFace LResNet100E-IR** (ONNX Model Zoo, opset 8) | 0.300 % | 0.300 % (9/2,999) | 0.433 % (13/2,999) | 197.3 / 247.3 ms | 261,036,388 B (248.9 MiB) |

Embedding time is one aligned 112x112 crop to a normalised embedding with OpenCV DNN on the CPU,
measured over 7,700 crops; detection and alignment are excluded. ArcFace's thresholds are on its own
score scale (EER at 0.2275), so 0.363 is not meaningful for it.

Charts (pairs where both faces were found): [DET](evaluation/det.svg), [ROC](evaluation/roc.svg).
Threshold sweeps: [`sface_thresholds.csv`](evaluation/sface_thresholds.csv),
[`arcface_thresholds.csv`](evaluation/arcface_thresholds.csv); per-pair scores:
[`pair_scores.csv`](evaluation/pair_scores.csv).

## Threshold recommendation (not applied)

- On LFW, the current **0.363** sits at the FAR <= 0.1 % operating point: the threshold chosen for that target
  is 0.3641, and the cross-validated thresholds range from 0.3367 to 0.3648. **No change is needed for a
  0.1 % target.**
- For a 1 % FAR target the threshold would be about **0.295** (FRR 0.77 % instead of 1.53 %). That trades
  roughly 15x more false accepts for half the false rejects. Not recommended for 1:N login.
- In 1:N identification the chance that an impostor matches *someone* grows roughly with the number of
  enrolled people (about N x the 1:1 FAR for small FAR), so a 1:N deployment needs a stricter, not looser,
  1:1 operating point. The ambiguity margin (0.035) is not evaluated here.

The threshold in code stays 0.363 until the owner decides otherwise.

## Limitations

- **LFW is not BioGate's use case.** It contains celebrity news photos, mostly frontal, low resolution, with
  a known demographic skew and identity overlap with many training sets. Webcam images, lighting, cameras and
  the enrolled population of a real deployment are different. These numbers are not a production FAR/FRR claim.
- 3,000 impostor pairs cannot measure FAR well below 0.1 %: FAR = 0.1 % is 3 pairs, and the fold-to-fold
  spread (±0.225 %) is larger than the target itself.
- Only 1:1 pair verification is measured; 1:N identification, the ambiguity margin, multi-sample templates
  and liveness are not.
- The production quality gate rejects 524 of 7,700 LFW images (low-resolution JPEGs). The "gate" column shows
  that on such images the gate, not the recognition model, dominates the error.
- The benchmark keeps images with several faces by choosing the central one; production rejects them.

## Licences

| Item | Licence / terms | Consequence |
|---|---|---|
| LFW images | No open licence was found. The images are news photographs whose copyright stays with the original owners; LFW is distributed for research. The primary terms page (`vis-www.cs.umass.edu/lfw/`) could not be reached from this machine, so the exact wording was **not verified** (see `docs/MANUAL_TODO.md`). | Used only as a local research benchmark; images are not committed or redistributed, only the download script, hashes and aggregate results. |
| YuNet | MIT (OpenCV Zoo) | used in production |
| SFace | Apache-2.0 (OpenCV Zoo) | used in production |
| ArcFace LResNet100E-IR (`arcfaceresnet100-8.onnx`, ONNX Model Zoo commit `4c46cd00`) | The ONNX Model Zoo page lists Apache-2.0, but the weights are InsightFace's model trained on MS1M (MS-Celeb-1M refined). InsightFace states that its training data and pretrained models are "available for non-commercial research purposes only", and MS-Celeb-1M itself was withdrawn by Microsoft in 2019. | Treated as **non-commercial, local comparison only**. The weights (SHA-256 `f3a6bc28…4e1b01da`) are never committed; `--arcface-model` points to a local file. |

## Recommendation on the model

Keep **SFace**. ArcFace R100 cut the error rates by roughly 2.5–3.5x on LFW, but it is 12x slower per
embedding, 6.7x larger, and its weights are restricted to non-commercial research use. A permissively
licensed ArcFace-class model would be worth re-running through the same protocol before any switch.
