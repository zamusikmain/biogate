"""Create or complete the local .env without printing or storing a plaintext password.

Existing non-empty values are never changed. Missing values are filled in:
- BIOGATE_ADMIN_USERNAME: --admin-username (default ``admin``);
- BIOGATE_ADMIN_PASSWORD_HASH: Argon2id hash of a password typed twice (or read from stdin);
- BIOGATE_SESSION_SECRET: a random 64-character URL-safe secret.
"""

from __future__ import annotations

import argparse
import secrets
import shutil
import sys
from getpass import getpass
from pathlib import Path

from argon2 import PasswordHasher, Type

from app.services.account_auth import AccountAuthError, AccountAuthService

USERNAME = "BIOGATE_ADMIN_USERNAME"
PASSWORD_HASH = "BIOGATE_ADMIN_PASSWORD_HASH"  # noqa: S105 -- variable name, not a value
SESSION_SECRET = "BIOGATE_SESSION_SECRET"  # noqa: S105 -- variable name, not a value


def read_values(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def set_value(lines: list[str], key: str, value: str) -> list[str]:
    updated = [f"{key}={value}" if line.strip().split("=", 1)[0].strip() == key else line for line in lines]
    if updated == lines:
        updated.append(f"{key}={value}")
    return updated


def ask_password(from_stdin: bool) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass("Administrator password: ")
        if getpass("Repeat password: ") != password:
            raise SystemExit("Passwords do not match.")
    try:
        AccountAuthService.validate_password(password)
    except AccountAuthError as error:
        raise SystemExit(
            f"{error.reason_code}: use at least 12 characters with lowercase, uppercase, digit and special character."
        ) from error
    return password


def bootstrap(env_path: Path, example_path: Path, username: str, password_from_stdin: bool) -> list[str]:
    if not env_path.exists():
        shutil.copyfile(example_path, env_path)
    lines = env_path.read_text(encoding="utf-8").splitlines()
    values = read_values(lines)
    filled: list[str] = []
    if not values.get(USERNAME):
        lines = set_value(lines, USERNAME, username)
        filled.append(USERNAME)
    if not values.get(PASSWORD_HASH):
        lines = set_value(lines, PASSWORD_HASH, PasswordHasher(type=Type.ID).hash(ask_password(password_from_stdin)))
        filled.append(PASSWORD_HASH)
    if not values.get(SESSION_SECRET):
        lines = set_value(lines, SESSION_SECRET, secrets.token_urlsafe(48))
        filled.append(SESSION_SECRET)
    if filled:
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return filled


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--example", type=Path, default=Path(".env.example"))
    parser.add_argument("--admin-username", default="admin")
    parser.add_argument("--password-stdin", action="store_true", help="read the administrator password from stdin")
    args = parser.parse_args()
    filled = bootstrap(args.env, args.example, args.admin_username, args.password_stdin)
    print(f"{args.env}: filled {', '.join(filled)}" if filled else f"{args.env}: already configured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
