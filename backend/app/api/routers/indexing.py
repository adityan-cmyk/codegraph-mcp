import asyncio
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from app.rag.indexing_service import IndexingSecurityError, index_rust_repository, replay_indexes_from_storage, semantic_rebuild_is_in_progress, reindex_semantic_only
from app.rag.ingestion.git_ingestor import ingest_incremental, get_last_indexed_commit, get_current_head
from app.rag.retrieval.graph import graph_index
from app.rag.retrieval.semantic import semantic_index
from app.schemas.codebase import GraphQueryResponse, IndexRepositoryRequest, IndexStats, IndexingResult, SemanticQueryRequest, SemanticMatch


router = APIRouter(prefix="/api/index", tags=["indexing"])


class NightlySyncFailure(BaseModel):
    stage: str
    error: str
    detail: str = ""


@router.post("/notify/nightly-sync-failed")
def notify_nightly_sync_failed(payload: NightlySyncFailure) -> dict:
    from app.core.notifications import notify_nightly_sync_failed
    notify_nightly_sync_failed(payload.stage, payload.error, payload.detail)
    return {"notified": True}


@router.post("/repository", response_model=IndexingResult)
async def index_repository(payload: IndexRepositoryRequest) -> IndexingResult:
    try:
        return await asyncio.to_thread(index_rust_repository, payload.repository_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository path was not found.") from exc
    except IndexingSecurityError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/semantic/rebuild", response_model=IndexingResult)
def rebuild_semantic_index() -> IndexingResult:
    result = reindex_semantic_only()
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No persisted index snapshot was found.")
    return result


@router.get("/stats", response_model=IndexStats)
def get_index_stats() -> IndexStats:
    semantic_stats = semantic_index.get_stats()
    graph_stats = graph_index.get_stats()
    return IndexStats(
        **semantic_stats,
        **graph_stats,
        semantic_rebuild_in_progress=semantic_rebuild_is_in_progress(),
    )


@router.post("/query", response_model=list[SemanticMatch])
def query_semantic_index(payload: SemanticQueryRequest) -> list[SemanticMatch]:
    return semantic_index.query_chunks(payload.query, limit=payload.limit)


@router.get("/graph/{symbol_id}", response_model=GraphQueryResponse)
def get_graph_neighborhood(symbol_id: str, depth: int = 1) -> GraphQueryResponse:
    if not semantic_index.has_symbol(symbol_id) and not graph_index.has_symbol(symbol_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Symbol was not found in the current index.")
    try:
        neighborhoods = graph_index.traverse(symbol_id, depth=depth)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return GraphQueryResponse(symbol_id=symbol_id, depth=depth, neighborhoods=neighborhoods)


@router.post("/replay", response_model=IndexingResult)
def replay_index_snapshot() -> IndexingResult:
    result = replay_indexes_from_storage()
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No persisted index snapshot was found.")
    return result


@router.post("/ingest")
def incremental_ingest() -> dict:
    try:
        return ingest_incremental()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@router.get("/ingest/status")
def ingest_status() -> dict:
    try:
        head = get_current_head()
    except Exception:
        head = "unknown"
    return {
        "last_indexed_commit": get_last_indexed_commit(),
        "current_head": head,
        "up_to_date": get_last_indexed_commit() == head,
    }