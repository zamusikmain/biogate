from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


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


class UserCreateRequest(BaseModel):
    external_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=100)
    status: str = Field(default="ACTIVE", pattern=r"^(ACTIVE|BLOCKED)$")
    comment: str = Field(default="", max_length=500)


class UserUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    status: str | None = Field(default=None, pattern=r"^(ACTIVE|BLOCKED)$")
    comment: str | None = Field(default=None, max_length=500)


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
