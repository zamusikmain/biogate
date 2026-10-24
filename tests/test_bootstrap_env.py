import io
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from scripts.bootstrap_env import bootstrap, read_values

ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_creates_env_with_hash_and_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    monkeypatch.setattr("sys.stdin", io.StringIO("Bootstrap-pass-123!\n"))
    filled = bootstrap(env, ROOT / ".env.example", "owner", password_from_stdin=True)
    # .env.example already names the administrator, so only the secrets are generated.
    assert filled == ["BIOGATE_ADMIN_PASSWORD_HASH", "BIOGATE_SESSION_SECRET"]
    values = read_values(env.read_text(encoding="utf-8").splitlines())
    assert values["BIOGATE_ADMIN_USERNAME"] == "admin"
    assert PasswordHasher().verify(values["BIOGATE_ADMIN_PASSWORD_HASH"], "Bootstrap-pass-123!")
    assert len(values["BIOGATE_SESSION_SECRET"]) >= 32
    assert "Bootstrap-pass-123!" not in env.read_text(encoding="utf-8")
    assert values["BIOGATE_ACTIVE_IMAGE_DAYS"] == "60"


def test_bootstrap_never_overwrites_existing_values(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "BIOGATE_ADMIN_USERNAME=keep\nBIOGATE_ADMIN_PASSWORD_HASH=existing\nBIOGATE_SESSION_SECRET=existing-secret\n",
        encoding="utf-8",
    )
    before = env.read_text(encoding="utf-8")
    assert bootstrap(env, ROOT / ".env.example", "admin", password_from_stdin=True) == []
    assert env.read_text(encoding="utf-8") == before


def test_bootstrap_rejects_weak_password(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    with pytest.raises(SystemExit, match="PASSWORD_TOO_SHORT"):
        bootstrap(tmp_path / ".env", ROOT / ".env.example", "admin", password_from_stdin=True)
