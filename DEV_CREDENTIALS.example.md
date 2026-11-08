# Development Credentials Template

Use this template only for disposable local development accounts. Copy it to
`DEV_CREDENTIALS.local.md` before filling in any local password. The local file
is ignored by Git; this public template must never contain a real password,
token, session secret, password hash, or production credential.

## Local environment

- Application URL: `http://127.0.0.1:8000/`
- Authentication type: local password authentication and local face authentication.
- External providers / OAuth: not implemented.

## Test accounts

### Administrator

- Login:
- Password:
- Role: `ADMIN`
- User ID:
- Database / environment:
- Login URL:
- Authentication method:
- Purpose:
- Account status:
- Password login enabled:
- Face login enabled:
- Credential status: `VERIFIED` / `NOT VERIFIED`

### Regular user

- Login:
- Password:
- Role: `USER`
- User ID:
- Database / environment:
- Login URL:
- Authentication method:
- Purpose:
- Account status:
- Password login enabled:
- Face login enabled:
- Credential status: `VERIFIED` / `NOT VERIFIED`

## Safe creation procedure

1. Create a local `.env` from `.env.example`.
2. Generate an Argon2id hash with `scripts/generate_admin_hash.py`; set the
   local bootstrap administrator values in `.env`, including a session secret.
3. Start the app with `Start.bat` and sign in at the local URL.
4. Use the ADMIN console to create disposable local `USER` or `ADMIN` accounts.
   The application requires a 12-character password with lowercase, uppercase,
   digit, and non-whitespace special character.
5. If the application generates a temporary password, it is shown once. Record
   it only in `DEV_CREDENTIALS.local.md`, never in this template or in Git.
6. Repeat the account section for every account, including multiple accounts with
   the same role. Mark `VERIFIED` only after a successful local login and role check.
7. Reset an unknown password only after confirming that the account belongs to a
   disposable local test environment and the reset is authorized. Preserve working
   or personal accounts whose test purpose cannot be confirmed.
8. Temporary pytest fixtures and synthetic UI sessions are not persistent password
   accounts. Document their lifecycle separately from usable local credentials.

## Safety notes

- Do not use OAuth credentials: BioGate does not implement OAuth or external identity providers.
- Do not recover, infer, or copy passwords from hashes.
- Do not include API keys, session secrets, reset tokens, biometric data, or production credentials.
- Do not commit `DEV_CREDENTIALS.local.md`.
