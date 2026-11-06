from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    service: str
    database: str
    model_loaded: bool


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
