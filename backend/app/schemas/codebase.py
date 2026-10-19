from datetime import datetime
import hashlib

from pydantic import BaseModel, Field

# Synthetic enrichment chunks — indexed for semantic search but never graph
# nodes. The symbol-set hash counts only graph-eligible symbols so the
# snapshot and the live graph hash the same universe.
GRAPH_SKIP_KINDS = frozenset({"file_summary", "module_exports", "mod"})
# Name-based graph exclusions (parser artifacts and trivial constructors) —
# must ALSO be hash-excluded or the two sides drift by their count (the
# 54-symbol permanent drift in symbol_sets_match).
_NAME_SKIP = frozenset({"Relation", "fn", "let", "match", "if"})


def is_graph_eligible(symbol_or_chunk) -> bool:
    """Exactly the graph-node eligibility predicate — hash and index share it."""
    if hasattr(symbol_or_chunk, "kind"):
        if symbol_or_chunk.kind in GRAPH_SKIP_KINDS:
            return False
    sid = getattr(symbol_or_chunk, "symbol_id", None) or str(symbol_or_chunk)
    short = sid.split("::")[-1]
    if short in _NAME_SKIP:
        return False
    if short == "new" and getattr(symbol_or_chunk, "kind", None) == "fn":
        content = (getattr(symbol_or_chunk, "content", "") or "").strip()
        if content and len(content) < 50:
            return False
    return True


def compute_symbol_set_hash(symbols) -> str:
    """sha256 over sorted unique symbol_ids — accepts chunks or plain ids.

    The fingerprint get_index_meta compares across the snapshot and the live
    graph: the commit pointer alone claimed freshness while incremental
    updates left deleted symbols behind. Uses is_graph_eligible on both sides
    so skip-kind chunks and name-based exclusions can't fake drift.
    """
    ids = sorted({
        (s.symbol_id if hasattr(s, "symbol_id") else str(s))
        for s in symbols
        if is_graph_eligible(s)
    })
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()


class CodeChunk(BaseModel):
    symbol_id: str
    file_path: str
    language: str = "rust"
    kind: str
    content: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    # True for #[test] fns and anything inside #[cfg(test)] modules — test
    # symbols stay in the index (get_tests_for_symbol needs them) but are
    # excluded from risk scoring.
    is_test: bool = False
    # "pub" | "pub_crate" | "private" — a private fn's blast radius is
    # contained by its module; a pub one's is not. Weighted into risk.
    visibility: str = "private"


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
    # sha256 over sorted unique symbol_ids at ingest time — get_index_meta
    # compares it against the live graph + snapshot so "up to date" can't
    # claim freshness while deleted symbols linger (commit pointer alone lied).
    symbol_set_hash: str = ""
    chunks: list[CodeChunk] = Field(default_factory=list)
    graph_edges: list[GraphEdge] = Field(default_factory=list)