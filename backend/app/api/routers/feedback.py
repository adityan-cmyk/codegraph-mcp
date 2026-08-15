"""Feedback API router — endpoints for external opencode clients.

External opencode agents that use our MCP tools can POST their post-analysis
feedback here. The feedback is stored in Postgres (not consumed immediately)
and later evaluated by the reinforcement agent's quality gate.
"""

import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.rag.reinforcement import ai_feedback_store, build_registry, feedback_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/feedback", tags=["feedback"])


class ToolCallSummary(BaseModel):
    tool: str
    args: dict = Field(default_factory=dict)
    result_summary: str = ""


class ResultUsedSummary(BaseModel):
    symbol_id: str = ""
    file_path: str = ""
    helpful: bool | None = None
    notes: str = ""


class AIFeedbackRequest(BaseModel):
    client_id: str | None = None
    pr_context: str | None = None
    tools_called: list[ToolCallSummary] = Field(default_factory=list)
    results_used: list[ResultUsedSummary] = Field(default_factory=list)
    results_expected: str | None = None
    quality_rating: int | None = Field(default=None, ge=1, le=5)
    improvement_suggestions: str | None = None


class SimpleFeedbackRequest(BaseModel):
    query_text: str
    symbol_id: str
    feedback: int = Field(ge=-1, le=1)
    original_score: float = 0.0
    reason: str = ""


@router.post("/ai")
async def submit_ai_feedback(req: AIFeedbackRequest) -> dict[str, object]:
    """Submit post-analysis feedback from an external AI agent.

    This is the main endpoint for opencode clients to report what they thought
    of the MCP tool outputs after completing a PR review. The feedback is stored
    with status='pending' and later evaluated by the quality gate.

    Fields:
    - client_id: identifier for the opencode client (optional)
    - pr_context: PR title or URL being reviewed (optional)
    - tools_called: list of {tool, args, result_summary} — what was called
    - results_used: list of {symbol_id, file_path, helpful, notes} — what was used
    - results_expected: what the agent expected but didn't find
    - quality_rating: 1-5 overall rating of MCP tool output quality
    - improvement_suggestions: what could be better
    """
    return ai_feedback_store.submit_feedback(
        client_id=req.client_id,
        pr_context=req.pr_context,
        tools_called=[t.model_dump() for t in req.tools_called],
        results_used=[r.model_dump() for r in req.results_used],
        results_expected=req.results_expected,
        quality_rating=req.quality_rating,
        improvement_suggestions=req.improvement_suggestions,
    )


@router.post("/simple")
async def submit_simple_feedback(req: SimpleFeedbackRequest) -> dict[str, object]:
    """Submit simple per-symbol feedback (lightweight version)."""
    feedback_store.record_feedback(
        query_text=req.query_text,
        symbol_id=req.symbol_id,
        original_score=req.original_score,
        feedback=req.feedback,
        reason=req.reason or None,
    )
    return {"status": "recorded", "query_text": req.query_text, "symbol_id": req.symbol_id}


@router.get("/ai/stats")
async def get_ai_feedback_stats() -> dict[str, object]:
    """Get AI feedback statistics — counts by status, average scores."""
    return ai_feedback_store.get_feedback_stats()


@router.get("/build/stats")
async def get_build_stats() -> dict[str, object]:
    """Get build registry statistics — active builds, rollbacks, quality scores."""
    return build_registry.get_build_stats()


@router.get("/build/history")
async def get_build_history() -> list[dict]:
    """Get build history — all builds with their status and quality scores."""
    return build_registry.get_build_history()


@router.post("/evaluate")
async def evaluate_pending_feedback() -> dict[str, object]:
    """Run quality gating on pending AI feedback. Accepts or rejects each entry."""
    return ai_feedback_store.evaluate_pending_feedback()


@router.get("/reinforcement/stats")
async def get_reinforcement_stats() -> dict[str, object]:
    """Get reinforcement learning statistics — boost weights, query expansions."""
    return feedback_store.get_reinforcement_stats()


@router.post("/build/rollback")
async def rollback_build(reason: str = "manual rollback") -> dict[str, object]:
    """Rollback to the previous build — restores old Weaviate collection and Neo4j gen.

    This is the idempotency mechanism: if a new build produces worse results,
    we revert to the previous build's data without re-indexing.
    """
    from app.rag.indexing_service import rollback_last_build
    return rollback_last_build(reason=reason)
