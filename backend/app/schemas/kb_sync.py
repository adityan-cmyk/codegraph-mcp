from typing import Literal

from pydantic import BaseModel, Field


class KbSyncRequest(BaseModel):
    old_commit: str = Field(min_length=4)
    new_commit: str = Field(min_length=4)
    repository_path: str | None = None


class KbSyncAcceptedResponse(BaseModel):
    task_id: str
    status: Literal["queued", "started", "completed"]
    old_commit: str
    new_commit: str


class KbSyncResult(BaseModel):
    task_id: str | None = None
    status: Literal["queued", "running", "completed", "failed"]
    old_commit: str
    new_commit: str
    repository_path: str
    modified_files: list[str] = Field(default_factory=list)
    modified_symbols: list[str] = Field(default_factory=list)
    impacted_symbols: list[str] = Field(default_factory=list)
    files_indexed: int = Field(default=0, ge=0)
    graph_edges: int = Field(default=0, ge=0)