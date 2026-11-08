# Needs the owner

Items Claude could not finish alone in the `polish-2` round.

1. **Accept the LFW terms.** Read on 2026-11-08 through the Internet Archive copy (the site itself still
   does not load): the page has no licence, only a request to cite the 2007 technical report and a
   disclaimer that LFW results do not show commercial suitability. Both are now in `docs/EVALUATION.md`.
   Decide whether local benchmarking and publishing aggregate numbers on that basis is acceptable to you.
2. **Decide the threshold.** On LFW the current 0.363 matches FAR <= 0.1 %; 0.295 would match FAR <= 1 %.
   The code is unchanged. See `docs/EVALUATION.md` → Threshold recommendation.
3. **Measure on your own camera.** LFW is not webcam data. Collect a consented set (several people, several
   sessions and lighting conditions), write a pairs file in the LFW format, run
   `python -m evaluation.run --pairs-file <pairs.txt> --images-dir <photos> --output-dir <outside docs/>`
   and record the numbers separately from LFW. Keep those photos out of Git.
4. **Check the new enrollment pose limits with a real camera.** Defaults: `BIOGATE_ENROLLMENT_MAX_YAW=0.35`,
   `BIOGATE_ENROLLMENT_MAX_ROLL_DEGREES=20`. Enroll once looking straight, once turned and once tilted, and
   confirm the turned/tilted frames are refused with `HEAD_TURNED` / `HEAD_TILTED` while normal frames pass.

   Pre-check without a camera (2026-11-08, production models, LFW images):
   - **Yaw works as intended.** All 251 of 11,562 detected faces above 0.35 were refused as `HEAD_TURNED`
     (2.2 % of mostly frontal news photos; 95th percentile |yaw| = 0.28). Mirroring an image flipped the
     sign of the yaw proxy in 300 of 300 cases.
   - **Roll does not.** 200 level, upright faces were rotated in-plane by ±5…30°. YuNet's eye landmarks stay
     close to horizontal on tilted faces: at a true 20° tilt the measured roll was typically 3–5°, so
     `HEAD_TILTED` almost never fired below 25° (at ±22°: 1 of 200 refused for tilt). Across 150 faces the
     YuNet roll error was 8–20° (median), while MediaPipe Face Landmarker eye corners on the same images were
     within 1° (95th percentile). Tilt also leaks into the YuNet yaw proxy (about +0.15 at 30°).
   - Suggested fix: measure roll from the MediaPipe landmarks the pipeline already loads. Not applied.
5. **ArcFace licence decision.** The compared ArcFace R100 weights are treated as non-commercial. If a
   permissively licensed ArcFace-class model is found, re-run the comparison before switching models.
6. From earlier rounds: live-face checks with a real camera and a face-login GIF (`docs/img/face-login.gif`).
   The README screenshots are in `docs/img/` (taken from the UI test harness), and CI runs on GitHub.
