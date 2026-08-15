from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.incident import IncidentFingerprint


class EvalCase(BaseModel):
    case_id: str
    fingerprint: IncidentFingerprint
    expected_root_cause: str
    expected_patch: str
    environment: str = Field(default="UAT")
    created_at: datetime | None = None
    tags: list[str] = Field(default_factory=list)


class EvalResult(BaseModel):
    case_id: str
    status: Literal["pass", "fail", "error"]
    actual_root_cause: str | None = None
    actual_patch: str | None = None
    error_detail: str | None = None
    confidence_scores: list[dict[str, str]] = Field(default_factory=list)


class EvalSuiteResult(BaseModel):
    suite_name: str
    total_cases: int
    passed: int
    failed: int
    errors: int
    results: list[EvalResult] = Field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None
