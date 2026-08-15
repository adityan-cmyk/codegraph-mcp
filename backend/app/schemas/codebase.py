from datetime import datetime

from pydantic import BaseModel, Field


class CodeChunk(BaseModel):
    symbol_id: str
    file_path: str
    language: str = "rust"
    kind: str
    content: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class SemanticMatch(BaseModel):
    symbol_id: str
    score: float
    content: str
    source: str


class GraphNeighborhood(BaseModel):
    symbol_id: str
    upstream: list[str] = Field(default_factory=list)
    downstream: list[str] = Field(default_factory=list)
    used_by: list[str] = Field(default_factory=list)
    uses: list[str] = Field(default_factory=list)
    used_by_modes: dict[str, list[str]] = Field(default_factory=dict)
    uses_modes: dict[str, list[str]] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    source_symbol_id: str
    target_symbol_id: str
    relation: str = "calls"
    usage_modes: list[str] = Field(default_factory=list)


class IndexingResult(BaseModel):
    symbols_indexed: int
    semantic_documents: int
    graph_nodes: int
    graph_edges: int
    files_indexed: int
    repository_path: str


class IndexRepositoryRequest(BaseModel):
    repository_path: str | None = None


class SemanticQueryRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=5, ge=1, le=20)


class IndexStats(BaseModel):
    semantic_documents: int
    resolved_error_documents: int
    graph_nodes: int
    graph_edges: int
    semantic_rebuild_in_progress: bool = False


class GraphQueryResponse(BaseModel):
    symbol_id: str
    depth: int = Field(ge=1)
    neighborhoods: list[GraphNeighborhood] = Field(default_factory=list)


class IndexSnapshot(BaseModel):
    repository_path: str
    files_indexed: int = Field(ge=0)
    created_at: datetime | None = None
    last_indexed_commit: str = ""
    chunks: list[CodeChunk] = Field(default_factory=list)
    graph_edges: list[GraphEdge] = Field(default_factory=list)