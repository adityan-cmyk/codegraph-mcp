from pydantic import BaseModel


class ConfidenceScore(BaseModel):
    label: str
    value: str


class SpanEvent(BaseModel):
    stage: str
    status: str
    detail: str