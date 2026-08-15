"""Read-only graph API router for opencode agent consumption.

Only exposes blast-radius and graph-traversal queries.
All endpoints are async and delegate to the thread pool so 10+ concurrent
requests don't block the FastAPI event loop.
"""

import asyncio

from fastapi import APIRouter, HTTPException, status

from app.rag.retrieval.graph import graph_index
from app.schemas.codebase import GraphNeighborhood, GraphQueryResponse

router = APIRouter(prefix="/api/graph", tags=["graph-read-only"])


@router.get("/blast-radius/{symbol_id}", response_model=GraphNeighborhood)
async def get_blast_radius(symbol_id: str) -> GraphNeighborhood:
    if not await asyncio.to_thread(graph_index.has_symbol, symbol_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Symbol '{symbol_id}' not found in the graph index.",
        )
    neighborhood = await asyncio.to_thread(
        lambda: graph_index.get_blast_radius(symbol_id)
        if hasattr(graph_index, "get_blast_radius")
        else graph_index.get_neighbors(symbol_id)
    )
    return neighborhood


@router.get("/traverse/{symbol_id}", response_model=GraphQueryResponse)
async def traverse_graph(symbol_id: str, depth: int = 2) -> GraphQueryResponse:
    clamped_depth = max(1, min(depth, 5))
    if not await asyncio.to_thread(graph_index.has_symbol, symbol_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Symbol '{symbol_id}' not found in the graph index.",
        )
    try:
        neighborhoods = await asyncio.to_thread(graph_index.traverse, symbol_id, clamped_depth)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return GraphQueryResponse(symbol_id=symbol_id, depth=clamped_depth, neighborhoods=neighborhoods)


@router.get("/stats")
async def get_graph_stats() -> dict[str, int]:
    return await asyncio.to_thread(graph_index.get_stats)


@router.get("/has/{symbol_id}")
async def has_symbol(symbol_id: str) -> dict[str, bool]:
    exists = await asyncio.to_thread(graph_index.has_symbol, symbol_id)
    return {"symbol_id": symbol_id, "exists": exists}
