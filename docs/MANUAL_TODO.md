# Needs the owner

Items Claude could not finish alone in the `polish-2` round.

1. **Check the LFW terms of use.** `vis-www.cs.umass.edu` did not resolve from this machine, so the official
   LFW page and its wording on research/commercial use were not read. Open http://vis-www.cs.umass.edu/lfw/
   and confirm that local benchmarking and publishing aggregate numbers is acceptable; adjust
   `docs/EVALUATION.md` → Licences if needed.
2. **Decide the threshold.** On LFW the current 0.363 matches FAR <= 0.1 %; 0.295 would match FAR <= 1 %.
   The code is unchanged. See `docs/EVALUATION.md` → Threshold recommendation.
3. **Measure on your own camera.** LFW is not webcam data. Collect a consented set (several people, several
   sessions and lighting conditions), write a pairs file in the LFW format, run
   `python -m evaluation.run --pairs-file <pairs.txt> --images-dir <photos> --output-dir <outside docs/>`
   and record the numbers separately from LFW. Keep those photos out of Git.
4. **Check the new enrollment pose limits with a real camera.** Defaults: `BIOGATE_ENROLLMENT_MAX_YAW=0.35`,
   `BIOGATE_ENROLLMENT_MAX_ROLL_DEGREES=20`. Enroll once looking straight, once turned and once tilted, and
   confirm the turned/tilted frames are refused with `HEAD_TURNED` / `HEAD_TILTED` while normal frames pass.
5. **ArcFace licence decision.** The compared ArcFace R100 weights are treated as non-commercial. If a
   permissively licensed ArcFace-class model is found, re-run the comparison before switching models.
6. From earlier rounds: live-face checks, screenshots/GIF in `docs/img/`, push and the first CI run.
