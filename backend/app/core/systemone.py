"""Local decision-model client (Ollama /v1/systemone, Jev-style).

Used by the feedback quality gate: instead of scoring feedback FORM (length,
symbol citations — gameable by any LLM), a local decision model (nimble 9B)
judges the CONTENT: is it specific, actionable, and internally consistent.

Measured on this host (i9-14900K, CPU-only): ~20-35s per decision,
memory-bandwidth-bound (does NOT scale with more CPUs). Fine for the async
reinforcement loop; never use on a hot request path.

Graceful degradation: every failure mode (service down, timeout, malformed
response) returns None — the caller falls back to the heuristic gate. The
index and feedback flow must never depend on this service being up.
"""

import json
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

# Acceptance rule: the actionable dimension is the hard gate. Feedback that
# doesn't describe expected-but-missing results is vacuous — it contributes
# nothing to query expansion or symbol boosts, no matter how specific it looks
# (verified against a form-perfect adversarial sample: specific=0.999 but
# actionable=0.04 — correctly rejected).
_ACTIONABLE_THRESHOLD = 0.5

# Guards for the generic make_decision tool
MAX_QUESTIONS = 8
MAX_STATE_CHARS = 8000
_ALLOWED_TYPES = ("noul", "score", "choice")

# Feedback routing taxonomy — what a feedback entry is ABOUT. Weight
# adjustment (boosts) only makes sense for ranking feedback; bug reports
# must not pollute symbol weights.
FEEDBACK_TYPES = ("ranking", "search_gap", "tool_bug", "index_gap")


def classify_feedback(feedback_row: dict) -> str:
    """Classify what a feedback entry is primarily about.

    Returns one of FEEDBACK_TYPES; 'ranking' (the historical behavior) on
    any failure — classification must never block the pipeline.
    """
    state = _build_state(feedback_row)
    suggestions = (feedback_row.get("improvement_suggestions") or "")[:500]
    expected = (feedback_row.get("results_expected") or "")[:500]
    state = (
        f"{state} Improvement suggestions: {suggestions or 'none'}. "
        f"Results expected: {expected or 'not stated'}."
    )
    try:
        answers = decide(
            state,
            {
                "kind": {
                    "type": "choice",
                    "instructions": (
                        "What is this feedback primarily about? "
                        "ranking = which search results/symbols were helpful or unhelpful; "
                        "search_gap = expected results were missing from search output; "
                        "tool_bug = a tool malfunctioned (noisy edges, wrong resolutions, false callers, broken aggregation); "
                        "index_gap = symbols or files are missing from the index itself."
                    ),
                    "criteria": {t: None for t in FEEDBACK_TYPES},
                }
            },
        )
        choice = (answers or {}).get("kind", {}).get("choice")
        if choice in FEEDBACK_TYPES:
            return choice
    except Exception:
        logger.debug("Feedback classification failed", exc_info=True)
    return "ranking"


def decide(state: str, questions: dict) -> dict | None:
    """Generic typed-decision call — returns the raw answers dict or None on
    failure (service down, timeout, malformed questions)."""
    if not settings.systemone_url:
        return None
    if not isinstance(state, str) or not state.strip():
        return None
    if not isinstance(questions, dict) or not questions or len(questions) > MAX_QUESTIONS:
        return None
    for name, q in questions.items():
        if not isinstance(q, dict) or q.get("type") not in _ALLOWED_TYPES:
            return None

    import requests

    try:
        response = requests.post(
            settings.systemone_url,
            json={"model": settings.systemone_model, "state": state, "questions": questions},
            timeout=settings.systemone_timeout,
        )
        response.raise_for_status()
        return response.json().get("answers") or None
    except Exception:
        logger.info("Decision call failed", exc_info=True)
        return None


def judge_feedback(feedback_row: dict) -> dict[str, float] | None:
    """Ask the decision model to judge a feedback entry.

    Returns {"specific": p, "actionable": p, "consistent": p} or None on any
    failure (service down, timeout, unexpected shape).
    """
    if not settings.systemone_url:
        return None

    import requests

    state = _build_state(feedback_row)
    payload = {
        "model": settings.systemone_model,
        "state": state,
        "questions": {
            "specific": {
                "type": "noul",
                "instructions": "Does this feedback reference specific symbols, files, or tool calls that were actually used?",
            },
            "actionable": {
                "type": "noul",
                "instructions": "Does the feedback clearly describe what results were expected but missing?",
            },
            "consistent": {
                "type": "noul",
                "instructions": "Is the quality rating consistent with the details described?",
            },
        },
    }

    try:
        response = requests.post(
            settings.systemone_url,
            json=payload,
            timeout=settings.systemone_timeout,
        )
        response.raise_for_status()
        answers = response.json().get("answers", {})
        judged: dict[str, float] = {}
        for key in ("specific", "actionable", "consistent"):
            value = answers.get(key, {}).get("noul")
            if not isinstance(value, (int, float)):
                return None
            judged[key] = float(value)
        return judged
    except Exception:
        logger.info(
            "Decision model unavailable (%s) — falling back to heuristic gate",
            settings.systemone_url,
        )
        return None


def gate_feedback(judged: dict[str, float]) -> tuple[float, str | None]:
    """Apply the acceptance rule to decision-model judgments.

    Returns (score 0-1, rejection_reason or None). Hard gate: actionable must
    be >= 0.5. The stored score blends all three dimensions.
    """
    if judged["actionable"] < _ACTIONABLE_THRESHOLD:
        reason = (
            f"decision-model gate: not actionable (actionable={judged['actionable']:.2f}, "
            f"specific={judged['specific']:.2f}, consistent={judged['consistent']:.2f})"
        )
        return judged["actionable"], reason
    score = 0.4 * judged["specific"] + 0.35 * judged["actionable"] + 0.25 * judged["consistent"]
    return score, None


def _build_state(feedback_row: dict) -> str:
    tools = feedback_row.get("tools_called") or []
    if isinstance(tools, str):
        tools = json.loads(tools)
    tool_names = [
        str((t.get("tool") if isinstance(t, dict) else None) or (t.get("name") if isinstance(t, dict) else None) or t)
        for t in tools
    ]

    results_used = feedback_row.get("results_used") or []
    if isinstance(results_used, str):
        results_used = json.loads(results_used)
    used_lines = []
    for r in results_used:
        if isinstance(r, dict):
            used_lines.append(f"{r.get('symbol_id', '?')} ({'helpful' if r.get('helpful') else 'not helpful'})")

    parts = [
        "PR review feedback from an AI agent.",
        f"Client: {feedback_row.get('client_id') or 'unknown'}.",
        f"PR context: {feedback_row.get('pr_context') or 'none'}.",
        f"Tools called: {', '.join(tool_names) if tool_names else 'none recorded'}.",
        f"Results used: {'; '.join(used_lines) if used_lines else 'none recorded'}.",
        f"Results expected: {feedback_row.get('results_expected') or 'not stated'}.",
        f"Quality rating: {feedback_row.get('quality_rating') or 'not given'}.",
        f"Improvement suggestions: {feedback_row.get('improvement_suggestions') or 'none'}.",
    ]
    return " ".join(parts)
