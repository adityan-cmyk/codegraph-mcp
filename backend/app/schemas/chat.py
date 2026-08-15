from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ChatThreadResponse(BaseModel):
    thread_id: str
    title: str
    created_at: datetime
    updated_at: datetime


class ChatMessageResponse(BaseModel):
    message_id: str
    thread_id: str
    role: str
    content: str
    citations: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime


class CreateThreadRequest(BaseModel):
    title: str = Field(default="New Chat", max_length=200)


class RenameThreadRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
