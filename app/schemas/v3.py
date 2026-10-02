from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator


class PasswordLoginRequest(BaseModel):
    login: str = Field(min_length=1, max_length=100)
    password: SecretStr


class PasswordChangeRequest(BaseModel):
    current_password: SecretStr
    new_password: SecretStr
    confirmation: SecretStr

    @model_validator(mode="after")
    def passwords_match(self) -> "PasswordChangeRequest":
        if self.new_password.get_secret_value() != self.confirmation.get_secret_value():
            raise ValueError("PASSWORD_CONFIRMATION_MISMATCH")
        return self


class PasswordResetRequest(BaseModel):
    token: SecretStr
    new_password: SecretStr
    confirmation: SecretStr

    @model_validator(mode="after")
    def passwords_match(self) -> "PasswordResetRequest":
        if self.new_password.get_secret_value() != self.confirmation.get_secret_value():
            raise ValueError("PASSWORD_CONFIRMATION_MISMATCH")
        return self


class AccountCreateRequest(BaseModel):
    external_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    login: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.@-]+$")
    full_name: str = Field(min_length=1, max_length=160)
    employee_id: str | None = Field(default=None, max_length=100)
    department: str = Field(default="", max_length=160)
    position: str = Field(default="", max_length=160)
    comment: str = Field(default="", max_length=1000)
    role: str = Field(default="USER", pattern=r"^(USER|ADMIN)$")
    status: str = Field(default="ACTIVE", pattern=r"^(ACTIVE|BLOCKED|DISABLED)$")
    password_enabled: bool = True
    face_enabled: bool = True
    biometric_recovery_enabled: bool = True
    temporary_password: SecretStr | None = None
    generate_temporary_password: bool = False

    @field_validator("external_id", "login", "full_name", mode="before")
    @classmethod
    def strip_required_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("employee_id", mode="before")
    @classmethod
    def normalize_employee_id(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("department", "position", "comment", mode="before")
    @classmethod
    def strip_optional_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class AccountUpdateRequest(BaseModel):
    login: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.@-]+$")
    full_name: str | None = Field(default=None, min_length=1, max_length=160)
    employee_id: str | None = Field(default=None, max_length=100)
    department: str | None = Field(default=None, max_length=160)
    position: str | None = Field(default=None, max_length=160)
    role: str | None = Field(default=None, pattern=r"^(USER|ADMIN)$")
    status: str | None = Field(default=None, pattern=r"^(ACTIVE|BLOCKED|DISABLED)$")
    password_enabled: bool | None = None
    face_enabled: bool | None = None
    biometric_recovery_enabled: bool | None = None
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("login", "full_name", "employee_id", "department", "position", "reason", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @model_validator(mode="after")
    def critical_changes_need_reason(self) -> "AccountUpdateRequest":
        if (self.status in {"BLOCKED", "DISABLED"} or self.role == "USER") and not (self.reason or "").strip():
            raise ValueError("ADMIN_ACTION_REASON_REQUIRED")
        return self


class AdminPasswordResetRequest(BaseModel):
    mode: str = Field(default="TEMPORARY", pattern=r"^(TEMPORARY|PERMANENT)$")
    temporary_password: SecretStr | None = None
    new_password: SecretStr | None = None
    confirmation: SecretStr | None = None
    generate: bool = False
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_reason(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @model_validator(mode="after")
    def validate_reset_mode(self) -> "AdminPasswordResetRequest":
        temporary = self.temporary_password.get_secret_value() if self.temporary_password else ""
        permanent = self.new_password.get_secret_value() if self.new_password else ""
        confirmation = self.confirmation.get_secret_value() if self.confirmation else ""
        if self.mode == "TEMPORARY":
            if self.generate == bool(temporary):
                raise ValueError("TEMPORARY_PASSWORD_REQUIRED")
            if permanent or confirmation:
                raise ValueError("PASSWORD_RESET_MODE_INVALID")
        else:
            if self.generate or temporary or not permanent:
                raise ValueError("PERMANENT_PASSWORD_REQUIRED")
            if permanent != confirmation:
                raise ValueError("PASSWORD_CONFIRMATION_MISMATCH")
        return self


class AdminReasonRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class ForcePasswordChangeRequest(BaseModel):
    enabled: bool = True


class RecoveryStartRequest(BaseModel):
    login: str = Field(min_length=1, max_length=100)


class BiometricReviewRequest(BaseModel):
    decision: str = Field(pattern=r"^(APPROVED|REJECTED|REVISION_REQUIRED)$")
    comment: str = Field(default="", max_length=1000)


class AdminNoteRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class SecurityReviewRequest(BaseModel):
    note: str = Field(default="", max_length=2000)


class BackupRestoreRequest(BaseModel):
    confirmation: str = Field(pattern=r"^RESTORE$")


class AdminSettingsRequest(BaseModel):
    allow_face_login: bool | None = None
    allow_password_login: bool | None = None
    account_session_ttl_seconds: int | None = Field(default=None, ge=300, le=7 * 24 * 60 * 60)
    account_session_idle_seconds: int | None = Field(default=None, ge=60, le=24 * 60 * 60)
    password_login_max_attempts: int | None = Field(default=None, ge=2, le=20)
    password_min_length: int | None = Field(default=None, ge=12, le=12)
    identification_threshold: float | None = Field(default=None, ge=-1.0, le=1.0)
    identification_ambiguity_margin: float | None = Field(default=None, ge=0.0, le=0.5)
    duplicate_biometric_threshold: float | None = Field(default=None, ge=-1.0, le=1.0)
    recovery_max_attempts: int | None = Field(default=None, ge=2, le=20)

    @model_validator(mode="after")
    def keep_authentication_available(self) -> "AdminSettingsRequest":
        if self.allow_face_login is False and self.allow_password_login is False:
            raise ValueError("GLOBAL_AUTH_METHOD_REQUIRED")
        return self
