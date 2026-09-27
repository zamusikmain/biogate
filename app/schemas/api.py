from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class HealthResponse(BaseModel):
    status: str
    service: str
    database: str
    model_loaded: bool


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    external_id: str
    display_name: str
    created_at: datetime
    enrolled_at: datetime | None
    status: str = "ACTIVE"
    comment: str = ""


class EnrollmentResponse(BaseModel):
    status: str
    user: UserResponse
    accepted_frames: int
    quality_score: float
    model_name: str


class VerificationResponse(BaseModel):
    face_detected: bool
    image_quality: bool
    liveness: bool
    liveness_score: float | None
    similarity_score: float | None
    threshold: float
    inference_time_ms: float
    result: str
    reason_code: str


class VerificationAttemptResponse(BaseModel):
    id: int
    claimed_external_id: str
    timestamp: datetime
    face_detected: bool
    quality_score: float | None
    liveness_score: float | None
    similarity_score: float | None
    threshold: float
    result: str
    reason_code: str
    inference_time_ms: float


class StatsResponse(BaseModel):
    enrolled_users: int = 0
    total_verifications: int = 0
    successful_verifications: int = 0
    rejected_verifications: int = 0
    average_inference_time_ms: float = 0.0
    evaluation_available: bool = False


class UserCreateRequest(BaseModel):
    external_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=100)
    status: str = Field(default="ACTIVE", pattern=r"^(ACTIVE|BLOCKED)$")
    comment: str = Field(default="", max_length=500)


class UserUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    status: str | None = Field(default=None, pattern=r"^(ACTIVE|BLOCKED)$")
    comment: str | None = Field(default=None, max_length=500)


class ChallengeCreateRequest(BaseModel):
    external_id: str = Field(min_length=1, max_length=64)


class ChallengeResponse(BaseModel):
    challenge_id: str
    sequence: list[str]
    current_step: int
    total_steps: int
    expected_action: str
    expires_at: datetime | None = None


class FrameStatusResponse(BaseModel):
    challenge_id: str
    sequence: list[str]
    current_step: int
    total_steps: int
    expected_action: str
    observed_action: str
    accepted: bool
    completed: bool
    feedback: str
    face_detected: bool
    quality_score: float
    yaw: float
    eye_aspect_ratio: float | None
    bounding_box: dict[str, float]
    attempt_id: int | None = None
    result: str | None = None
    reason_code: str | None = None
    similarity_score: float | None = None
    threshold: float | None = None
    liveness_score: float | None = None
    inference_time_ms: float | None = None
    debug: dict[str, bool | float | int | str | None] | None = None


class PhotoResult(BaseModel):
    index: int
    filename: str
    accepted: bool
    reason_code: str
    quality_score: float | None = None


class PhotoEnrollmentResponse(BaseModel):
    status: str
    accepted_frames: int
    required_frames: int
    template_created: bool
    results: list[PhotoResult]
    model_name: str | None = None


class EnrollmentFrameValidationResponse(BaseModel):
    accepted: bool
    reason_code: str
    quality_score: float | None = None
    yaw: float | None = None
    bounding_box: dict[str, float] | None = None


class SettingUpdateRequest(BaseModel):
    store_attempt_images: bool


class DeleteResponse(BaseModel):
    deleted: bool


class ErrorResponse(BaseModel):
    detail: str
    reason_code: str


class PaginationQuery(BaseModel):
    limit: int = Field(default=100, ge=1, le=500)


class AdminLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: SecretStr


class AdminAuthResponse(BaseModel):
    authenticated: bool
    username: str
    csrf_token: str
