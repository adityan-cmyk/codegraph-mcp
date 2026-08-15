from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.state_machine import IncidentState
from app.schemas.telemetry import ConfidenceScore


class ChatMessageRequest(BaseModel):
    role: str = Field(description="The role of the message sender (user, assistant, system)")
    content: str = Field(description="The message content")


class ModelChatRequest(BaseModel):
    message: str = Field(min_length=1, description="The user message to send to the model")
    thread_id: str | None = Field(default=None, description="Thread to associate the message with")
    history: list[ChatMessageRequest] = Field(default_factory=list, description="Previous messages in the conversation")


class Citation(BaseModel):
    symbol_id: str
    file_path: str
    score: float
    snippet: str = ""


class ModelChatResponse(BaseModel):
    reply: str
    model: str
    citations: list[Citation] = Field(default_factory=list)


class IncidentFingerprint(BaseModel):
    service: str
    panic_type: str
    top_frame: str
    commit_hash: str


class CreateIncidentRequest(BaseModel):
    fingerprint: IncidentFingerprint
    environment: str = Field(default="UAT")
    build_id: str = Field(min_length=1)
    raw_log: str = Field(min_length=1)
    source: str = Field(default="manual")


class IncidentEvent(BaseModel):
    event_id: str
    session_id: str
    event_type: str
    state: IncidentState
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class IncidentSession(BaseModel):
    session_id: str
    fingerprint: IncidentFingerprint
    environment: str
    build_id: str
    raw_log: str
    source: str
    state: IncidentState
    created_at: datetime
    updated_at: datetime
    timeline: list[IncidentEvent] = Field(default_factory=list)


class UpdateIncidentStateRequest(BaseModel):
    next_state: IncidentState
    event_type: str = Field(default="state_transition")
    payload: dict[str, Any] = Field(default_factory=dict)


class AnalyzeIncidentRequest(BaseModel):
    graph_depth: int = Field(default=2, ge=1, le=6)
    token_budget: int = Field(default=4096, ge=256, le=32768)
    deployment_window: str = Field(default="24h", min_length=2, max_length=32)
    confidence_threshold: float = Field(default=0.8, ge=0.0, le=1.0)


class IncidentAnalysis(BaseModel):
    root_cause: str
    patch: str
    confidence: list[ConfidenceScore]
    context: dict[str, Any] = Field(default_factory=dict)


class AnalyzeIncidentResponse(BaseModel):
    session: IncidentSession
    analysis: IncidentAnalysis


class ResolutionPackage(BaseModel):
    fingerprint: IncidentFingerprint
    root_cause: str
    patch: str