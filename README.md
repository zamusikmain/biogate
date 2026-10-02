# BioGate V3

BioGate V3 is a standalone, local-first biometric identity and access management system. It combines password authentication, face login without preselecting an account, a separate USER application, an ADMIN console, approval-based biometric updates, evidence storage, audit/security events, and local backup/restore.

The normal V3 lifecycle uses one account, role, server-side session, and backend RBAC model. `/` is the unified password/face entry point; USER accounts are routed to `/user` and ADMIN accounts to `/admin`. `/admin/login` and the legacy admin API remain only for V2 compatibility and cannot open or authorize the V3 ADMIN console.

It is designed for Windows and CPU execution with SQLite. It has no required cloud API, paid service, Docker, Kubernetes, or IncidentAI dependency.

> Active liveness is a demonstration mechanism, not certified Presentation Attack Detection (PAD). BioGate is not presented as regulatory, GDPR, ISO, or production security compliance.

## What V3 provides

- Unified login page with password login, 1:N face login, and biometric password recovery.
- `USER` and `ADMIN` roles; `ACTIVE`, `BLOCKED`, and `DISABLED` account states.
- Argon2id password hashes, password policy, failed-login throttling, temporary passwords, mandatory first-login password replacement, and administrator reset.
- Persisted server-side sessions with opaque signed HttpOnly cookies, `SameSite=Strict`, CSRF validation, absolute expiry, idle timeout, device metadata, logout, and revocation.
- 1:N local identification with a configurable threshold and Top-1/Top-2 ambiguity margin.
- Explicit `IDENTIFIED`, `UNKNOWN`, `AMBIGUOUS`, `BLOCKED`, and `DISABLED` decisions.
- Duplicate-biometric detection before a candidate template can be approved.
- User biometric update workflow: `PENDING_REVIEW`, `APPROVED`, `REJECTED`, `REVISION_REQUIRED`, and `CANCELLED`.
- Selected evidence photos for access and biometric-request events; no continuous video.
- Active evidence storage for exactly 60 days followed by permanent local archival.
- SHA-256 evidence integrity checks, security alerts, and protected photo endpoints.
- Access events, security events, audit history, notifications, account sessions, user timelines, and internal administrator notes.
- ADMIN live access monitor over SSE, CSV export, backup creation/validation, and explicitly confirmed restore with a safety backup.
- Live Russian/English localization and persistent Light/Dark/System themes across the unified login, USER, and ADMIN interfaces.
- Additive, idempotent V2 → V3 SQLite migration.

## Architecture

```text
Browser
  ├─ Login / USER UI / ADMIN UI
  ├─ unmirrored JPEG uploads (preview alone is mirrored)
  ▼
FastAPI
  ├─ account auth + persisted sessions + CSRF + RBAC
  ├─ server-owned liveness challenge
  ├─ 1:N identification / targeted 1:1 recovery
  ├─ biometric approval workflow
  ├─ evidence, archive, integrity, backup
  └─ audit, access, security, notifications
  ▼
SQLite + protected local storage
```

The established CV stack is unchanged:

- OpenCV YuNet face detection;
- OpenCV SFace alignment and embedding;
- MediaPipe Face Landmarker blink geometry;
- blur, lighting, face-count, face-size, and quality gates;
- neutral `CENTER` calibration;
- semantic physical `TURN_LEFT` and `TURN_RIGHT` mapping;
- `BLINK` open → closed → open validation;
- randomized backend-owned sequence, stability frames, cooldown, and expiration.

The browser never submits a trusted `livenessPassed` value. The backend owns sequence, current action, accepted samples, expiry, and final decision.

## Face login and 1:N identification

The user selects “Sign in with face” without choosing an account. The backend creates a randomized liveness challenge. After completion, accepted frame embeddings are aggregated in memory and compared with all templates belonging to `ACTIVE`, `BLOCKED`, or `DISABLED` accounts.

The decision uses both:

1. `BIOGATE_IDENTIFICATION_THRESHOLD` (default `0.363`);
2. `BIOGATE_IDENTIFICATION_AMBIGUITY_MARGIN` between Top-1 and Top-2.

Below threshold is `UNKNOWN`. A close Top-1/Top-2 result is `AMBIGUOUS`. Neither receives a session. A confidently recognized blocked or disabled account produces its corresponding policy decision, evidence, access event, and security alert without creating a session. Candidate identities are retained only in protected security metadata and are not exposed in the public failure response.

`0.363` remains the established SFace cosine threshold. No FAR, FRR, or accuracy is claimed. Any real 1:N deployment requires representative evaluation using its population, cameras, lighting, and attack model.

## Password authentication and recovery

Passwords are hashed with Argon2id; plaintext passwords and hashes are never returned by account APIs. A normal password change requires the current password and revokes the user’s other sessions while keeping the current session.

Every password-setting path uses one backend policy: at least 12 characters with at least one ASCII lowercase letter, one ASCII uppercase letter, one ASCII digit, and one non-alphanumeric, non-whitespace special character. Unicode is allowed as additional content, but it does not replace the required ASCII letter/digit categories, and whitespace does not count as a special character. The same validation applies to account creation, manually entered or generated temporary passwords, administrator permanent resets, user password changes, forced first-login changes, and biometric recovery resets. USER and ADMIN forms show the same localized requirements and specific failure messages before submission; the backend remains authoritative.

An administrator can set or generate a temporary password. Only the hash is stored. A generated value is returned once in the creation/reset response, `must_change_password` is enabled, and existing sessions are revoked. Until the user replaces it, full USER/ADMIN endpoints reject the session.

“Forgot password?” is a targeted 1:1 flow:

1. The user supplies a login.
2. The public response does not disclose whether that account exists or has biometrics.
3. The user completes backend-owned liveness.
4. The aggregate is compared only with that account’s active template.
5. Success creates a short-lived, single-use reset authorization whose token is stored only as a hash.
6. A successful reset revokes every account session and emits audit, security, and notification events.

Recovery starts and password attempts are rate-limited. Security questions, email, and SMS are intentionally absent.

## Sessions, CSRF, and authorization

The `biogate_session` cookie contains an opaque session ID/token pair plus an HMAC signature. Only the token hash is stored in SQLite. Session records contain creation/login method, absolute expiry, idle expiry, last-seen time, user agent, client address, and revocation state.

ADMIN user management includes a hash-routed User Card with profile, biometric state, user-specific access/security events, sessions, timeline, internal notes, and per-account password/face/recovery switches. Critical lifecycle changes require a reason, revoke affected sessions, and are audited. Backend checks ensure that at least one active ADMIN account remains and prevent an active account from losing every sign-in method accidentally.

All changing authenticated requests require the session CSRF token in `X-CSRF-Token`. Backend dependencies enforce ownership and ADMIN permissions; hiding UI controls is not treated as authorization. Users can only read their own profile, access events, notifications, biometric request, photos, and sessions. Protected evidence is outside `static/` and is served only after an authorization check.

## Account creation and administration

An ADMIN creates USER or ADMIN accounts from **Create user**. Identifiers are trimmed on the backend; login matching remains intentionally case-sensitive, so `Test` and `test` are distinct accounts. Login, external ID, and employee ID uniqueness is enforced by SQLite and returned as localized conflicts rather than raw database errors.

A supplied temporary password is validated and stored only as an Argon2id hash. If the field is empty, BioGate generates a strong value, returns it once, and never records the plaintext in SQLite, audit, or logs. Account creation, its `ACCOUNT_CREATED` audit entry, and notification are one transaction. Success opens the new hash-routed User Card; errors preserve the safe form fields and remain in Create user. The first password sign-in requires a password change.

Growing ADMIN and USER tables use bounded server-side `limit`/`offset` queries with Previous/Next controls. Search and filters are retained while paging and reset to the first page when changed. Access, security, audit, and unknown-face records expose a reusable Event Details view whose metadata is recursively stripped of session identifiers, tokens, secrets, hashes, and biometric vectors/templates.

## Biometric update workflow

Users submit 3–5 uploaded or automatically captured webcam samples. Every image is checked for MIME, encoded size, decodability, face count, blur, lighting, alignment, and embedding extraction. The aggregate candidate is compared with other active templates for possible duplicates.

The existing active template remains usable until an administrator approves the request. Rejection or a revision request never replaces it. Revision requires an administrator comment and permits resubmission. Approval atomically upserts the active template and records history, audit, and notification events. Raw embeddings and templates are never returned by the API.

## Evidence and archive

BioGate stores selected evidence images for successful and denied face flows, unknown/ambiguous faces, blocked/disabled attempts, recovery, and biometric requests. It never stores continuous webcam video.

Each record has a UUID photo ID and UUID filename, links to account/access/request where applicable, MIME, byte size, SHA-256, storage state, and integrity state. Files begin in `data/active_images/`. Once older than 60 days, an idempotent archival job moves them to `data/archive/` and retains them permanently. This permanent retention has significant privacy and storage implications; operators must define a lawful, proportionate policy for their environment.

An integrity mismatch becomes `INTEGRITY_FAILED` and creates both audit and critical security records. BioGate does not silently repair altered evidence.

Preview responses use the recorded image MIME inline. Explicit downloads reuse the same authorization, path, and integrity checks but set an attachment disposition with a bounded filename such as `biogate_evidence_20260929_ab12cd34ef56.jpg`; internal UUID storage names are not renamed. Sensitive V3/admin responses and evidence use `Cache-Control: no-store, private`.

## Backup and restore

ADMIN can create a ZIP containing a consistent SQLite backup plus active and archived evidence. `.env`, passwords, session secrets, and other plaintext secrets are not included. Every new backup uses format version 1 and includes `manifest.json` with its USER/SAFETY kind, schema version, creation time, and the size and SHA-256 of every included file.

Restore is never automatic. It requires the exact confirmation value `RESTORE`. Before any current state is changed, BioGate validates the archive and manifest, rejects missing/unlisted or path-traversal entries, verifies every file SHA-256, checks schema compatibility, and runs SQLite `PRAGMA integrity_check`. Only then does it create a safety backup and begin replacement. Evidence is restored only under its protected roots.

Safety backups are classified as internal recovery artifacts and do not appear in the ordinary list of explicitly created user backups, avoiding restore → backup list clutter while retaining emergency rollback capability.

Backups made before the versioned manifest are detected as legacy and rejected with a controlled compatibility error. They are never silently interpreted as valid current backups.

## Rule-based risk engine

The ADMIN Event Log includes deterministic, event-based risk classification. It is not ML and does not inspect a face, appearance, demographic attributes, emotion, or inferred human behavior. Centralized rules evaluate only technical security events in bounded windows: repeated failed password logins, unknown-face attempts, biometric/liveness failures, recovery abuse, blocked/disabled account attempts, and excessive password resets. Results are `LOW`, `MEDIUM`, `HIGH`, or `CRITICAL`, with a machine-readable reason and localized RU/EN presentation. When no rule escalates an event, no extra risk block clutters Event Details.

## Offline biometric benchmark

[`scripts/evaluate_biometrics.py`](scripts/evaluate_biometrics.py) is an explicit developer utility. It never runs with the application, reads no production database/evidence automatically, sends nothing to the network, and never changes production configuration. Supply a consented non-production dataset arranged as one directory per identity:

```text
dataset/
  person_001/1.jpg
  person_001/2.jpg
  person_002/1.jpg
  person_002/2.jpg
```

Example:

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_biometrics.py --dataset C:\path\to\consented-dataset --threshold 0.40 --output-json report.json --output-csv thresholds.csv
```

The tool reuses the production detector/alignment/SFace embedding and cosine-similarity services. It reports identities, images, genuine/impostor pair counts and distributions, false accepts/rejects for requested thresholds, Top-1/Top-2 margin statistics where meaningful, and always marks the current `0.363` production threshold separately. It does not modify that threshold.

This is local offline evaluation on the supplied dataset. Results depend on that dataset and are not a certified biometric evaluation, production-grade FAR/FRR evidence, or PAD certification.

## OpenAPI

`/docs` documents the consumer-facing BioGate V3 API grouped into Authentication, Users, Biometrics, Access, Evidence, Sessions, Admin, Backups, and Health. Controlled error status codes are included. Legacy and implementation-specific compatibility endpoints continue to operate but are intentionally omitted from `/openapi.json`.

## Localization, themes, and responsive UI

The unified login, USER application, and ADMIN console share the browser preferences `biogate-language` and `biogate-theme`. Russian/English switches rerender the active view without logout or reload, including dynamic tables, status labels, biometric requests, notifications, dialogs, validation messages, and camera guidance. Machine-readable database/API codes remain unchanged and are translated only at presentation time.

Light forces the light palette, Dark forces the dark palette, and System follows `prefers-color-scheme`. Preferences survive navigation, refresh, logout, and the next login because they remain in local browser storage; no account-profile persistence is required. USER controls are available in the common header and remain keyboard-accessible and usable at the 390 px mobile breakpoint.

## SQLite migration

Startup runs additive `CREATE TABLE IF NOT EXISTS` and `ALTER TABLE ADD COLUMN` operations, backfills legacy `login` and `full_name`, creates unique indexes, and sets `PRAGMA user_version=3`. Existing V2 users, templates, verification attempts, audit events, session history, and snapshot metadata are not dropped. Migration is idempotent and covered by a legacy-schema preservation test.

Before first V3 use with important data, make an offline copy of `data/biogate.db` and the `data/` directory.

## Setup on Windows

Requires Python 3.11–3.13.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Generate an Argon2id administrator hash without placing the password in shell history:

```powershell
.\.venv\Scripts\python.exe scripts\generate_admin_hash.py
```

The helper prompts twice without echoing input. Put only its resulting hash in `.env`; do not commit `.env`.

At minimum configure:

```dotenv
BIOGATE_ADMIN_USERNAME=admin
BIOGATE_ADMIN_PASSWORD_HASH=<argon2id hash>
BIOGATE_SESSION_SECRET=<random secret of at least 32 characters>
```

The environment administrator is additively bootstrapped into the V3 account table on startup if necessary. An existing V3 password is not overwritten on later starts. This is the local first-ADMIN bootstrap mechanism: it is configuration-driven rather than a public registration endpoint, and normal API account creation remains ADMIN-only.

The ADMIN dashboard exposes real database/model readiness plus evidence counts and byte totals sourced from SQLite metadata, avoiding a full filesystem scan on every refresh. Backup management supports create, list, integrity validation, confirmed restore with a safety copy, and reasoned/audited deletion.

Run:

```powershell
.\Start.bat
```

`Start.bat` starts Uvicorn, waits about five seconds, and opens `http://127.0.0.1:8000/` without administrator rights. Alternatively:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

## Configuration

See `.env.example`. Important V3 values include identification threshold/margin, duplicate threshold, account absolute/idle session timeouts, password policy and throttling, recovery limits, protected storage roots, and login-method switches. Active evidence retention is deliberately fixed at 60 days. Liveness remains mandatory for face login and biometric recovery.

The real `.env`, SQLite files, model binaries, evidence images, archives, and backups are excluded from Git.

## Checks

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy --strict app scripts\evaluate_biometrics.py
Get-ChildItem app\static -Filter *.js | ForEach-Object { node --check $_.FullName }
```

The tests cover preserved V2 behavior, head-pose semantics, authentication/CSRF/RBAC, migration, identification threshold and ambiguity behavior, blocked accounts, throttling, the unified password policy, temporary/permanent password flows, USER/ADMIN localization and themes, biometric request revision/resubmission, evidence archival/integrity, versioned backup validation, deterministic risk rules, offline benchmark metrics, and OpenAPI filtering.

## Face capture guidance

Login, admin webcam enrollment and user biometric update share the scoped
`FaceGuidance` component. Its head-shaped SVG and instructions are separate from
server-owned liveness/identification. The mirrored preview does not mirror the
JPEG submitted for analysis. LEFT/RIGHT remain physical head directions.

Intermediate face-login responses expose existing face geometry and calibration
feedback, not embeddings. Position arrows account for `object-fit: cover` and the
mirrored preview. The guide's visual alignment corridor (26 horizontal / 24 vertical
units in its 320×240 viewBox) is UX-only and never changes acceptance or thresholds.
Green requires a server-accepted step or final result. `FACE_TOO_SMALL` controls
move-closer guidance; there is no invented move-farther policy.

Camera readiness starts analysis automatically. Cancel/Retry abort outstanding
requests, stop tracks and reject stale responses. Retry creates a new challenge;
abandoned server challenges retain their existing TTL. Accepted actions have a
450 ms visual confirmation. The final frame request shows processing while keeping
its instruction visible: liveness completion and identification share one response.

Node.js is required for executable frontend regressions run by pytest. For isolated
browser QA, run `.venv\\Scripts\\python.exe -m tests.ui.qa_server` and open
`http://127.0.0.1:8765/`. This test-only server injects synthetic video/responses into
the actual templates and uses a temporary database and data directories. It is
never imported or enabled by `app.main:app`. Stop it after QA. UI simulation does
not validate physical camera quality, recognition or liveness; these require manual QA.

## Privacy model and limitations

BioGate V3 stores aggregate biometric templates, access metadata, selected evidence, biometric-request images, unknown/ambiguous evidence, permanent archives, sessions, and audit/security metadata. It does not store continuous video, plaintext passwords, plaintext reset/session tokens, password hashes in APIs, templates in APIs, or raw embeddings in logs.

SQLite and local filesystem storage are intended for a single local instance, not horizontal scaling. In-memory active liveness state is lost on restart and the challenge must be repeated. SSE monitor interruption does not affect authentication. The bundled liveness is not certified PAD, 1:N performance is not asserted without representative evaluation, and permanent evidence retention must be treated as a deliberate privacy decision.
