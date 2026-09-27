from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BIOGATE_", env_file=".env", extra="ignore")

    db_path: Path = Path("data/biogate.db")
    model_cache: Path = Path("model_cache")
    verification_threshold: float = Field(default=0.363, ge=-1.0, le=1.0)
    max_image_size: int = Field(default=5 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    min_enrollment_frames: int = Field(default=3, ge=2, le=10)
    max_enrollment_frames: int = Field(default=5, ge=2, le=10)
    min_face_pixels: int = Field(default=80, ge=32)
    min_sharpness: float = Field(default=55.0, ge=0)
    min_brightness: float = Field(default=45.0, ge=0, le=255)
    max_brightness: float = Field(default=215.0, ge=0, le=255)
    challenge_ttl_seconds: int = Field(default=120, ge=30, le=600)
    challenge_cooldown_ms: int = Field(default=550, ge=200, le=3000)
    head_baseline_frames: int = Field(default=3, ge=2, le=8)
    head_stability_frames: int = Field(default=2, ge=2, le=8)
    head_center_dead_zone: float = Field(default=0.06, ge=0.02, le=0.15)
    head_turn_delta: float = Field(default=0.16, ge=0.06, le=0.30)
    head_calibration_range: float = Field(default=0.05, ge=0.01, le=0.15)
    head_max_center_yaw: float = Field(default=0.20, ge=0.10, le=0.40)
    store_attempt_images: bool = False
    attempt_image_dir: Path = Path("data/attempt_snapshots")
    attempt_image_retention_days: int = Field(default=7, ge=1, le=365)
    admin_username: str = ""
    admin_password_hash: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")
    admin_session_ttl_seconds: int = Field(default=8 * 60 * 60, ge=300, le=7 * 24 * 60 * 60)
    admin_cookie_secure: bool = False
    admin_login_max_attempts: int = Field(default=5, ge=2, le=20)
    admin_login_window_seconds: int = Field(default=5 * 60, ge=30, le=60 * 60)
    admin_login_cooldown_seconds: int = Field(default=60, ge=10, le=60 * 60)
    debug: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
