"""Postgres-backed store for AI agent feedback from external opencode clients.

When an external opencode agent completes a PR analysis using our MCP tools,
it POSTs a feedback summary to our feedback endpoint. We store it here
with a 'pending' status — it is NOT consumed immediately. A quality-gating
mechanism later evaluates which feedback is valuable and accepts only those.

Accepted feedback is used to:
  1. Adjust per-symbol boost/penalty weights (reranking layer)
  2. Identify query expansion opportunities
  3. Trigger new builds with improved parameters
  4. Evaluate build quality for rollback decisions
"""

import json
import logging
import threading
import uuid
from datetime import UTC, datetime

import psycopg
from psycopg.rows import dict_row

from app.core.config import settings

logger = logging.getLogger(__name__)

_DSN = settings.postgres_dsn
_schema_lock = threading.Lock()
_schema_ready = False


def _connect():
    return psycopg.connect(_DSN, row_factory=dict_row)


def _ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ai_feedback (
                        id                    SERIAL PRIMARY KEY,
                        feedback_id           TEXT UNIQUE NOT NULL,
                        client_id             TEXT,
                        pr_context            TEXT,
                        tools_called           JSONB NOT NULL DEFAULT '[]',
                        results_used          JSONB NOT NULL DEFAULT '[]',
                        results_expected      TEXT,
                        quality_rating         SMALLINT,
                        improvement_suggestions TEXT,
                        status                TEXT NOT NULL DEFAULT 'pending',
                        quality_score         DOUBLE PRECISION,
                        rejection_reason       TEXT,
                        created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        evaluated_at          TIMESTAMPTZ,
                        consumed_at           TIMESTAMPTZ,
                        consumed_by_build     TEXT
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS ai_fb_status_idx ON ai_feedback(status)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS ai_fb_created_idx ON ai_feedback(created_at)"
                )
            conn.commit()
        _schema_ready = True
        logger.info("AI feedback schema ready")


def submit_feedback(
    client_id: str | None,
    pr_context: str | None,
    tools_called: list[dict],
    results_used: list[dict],
    results_expected: str | None,
    quality_rating: int | None,
    improvement_suggestions: str | None,
) -> dict[str, object]:
    """Store a new AI feedback entry with status='pending'."""
    _ensure_schema()
    feedback_id = uuid.uuid4().hex[:16]

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ai_feedback (
                    feedback_id, client_id, pr_context, tools_called,
                    results_used, results_expected, quality_rating,
                    improvement_suggestions, status, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
                RETURNING id
                """,
                (
                    feedback_id,
                    client_id,
                    pr_context,
                    json.dumps(tools_called),
                    json.dumps(results_used),
                    results_expected,
                    quality_rating,
                    improvement_suggestions,
                    datetime.now(UTC),
                ),
            )
            row = cur.fetchone()
        conn.commit()

    logger.info(
        "AI feedback submitted: id=%s client=%s rating=%s tools=%d",
        feedback_id, client_id, quality_rating, len(tools_called),
    )
    return {"feedback_id": feedback_id, "status": "pending", "db_id": row["id"]}


def _evaluate_quality(feedback_row: dict) -> tuple[float, str | None]:
    """Quality gate — returns (score 0.0-1.0, rejection_reason or None).

    Only feedback with score > 0.5 is accepted. The rest is rejected.
    """
    score = 0.0
    reasons = []

    tools = feedback_row.get("tools_called") or []
    if isinstance(tools, str):
        tools = json.loads(tools)

    results_used = feedback_row.get("results_used") or []
    if isinstance(results_used, str):
        results_used = json.loads(results_used)

    suggestions = feedback_row.get("improvement_suggestions") or ""
    expected = feedback_row.get("results_expected") or ""
    rating = feedback_row.get("quality_rating")

    # 1. Tool usage check — did the agent actually use tools?
    if tools and len(tools) > 0:
        score += 0.15
    else:
        reasons.append("no tools were called")

    # 2. Specificity check — does feedback mention specific symbols/files?
    specific_mentions = 0
    for r in results_used:
        if isinstance(r, dict) and (r.get("symbol_id") or r.get("file_path")):
            specific_mentions += 1
    if specific_mentions > 0:
        score += min(0.20, 0.05 * specific_mentions)
    else:
        reasons.append("no specific symbol or file references")

    # 3. Constructiveness check — does it provide improvement suggestions?
    if suggestions and len(suggestions.strip()) > 30:
        score += 0.20
    elif suggestions and len(suggestions.strip()) > 10:
        score += 0.10
    else:
        reasons.append("no actionable improvement suggestions")

    # 4. Expected-results check — does it say what was missing?
    if expected and len(expected.strip()) > 20:
        score += 0.15
    else:
        reasons.append("no expected-results description")

    # 5. Rating consistency — does the rating match the feedback content?
    if rating is not None:
        if 1 <= rating <= 5:
            score += 0.10
            # Low rating with specific suggestions = high-value feedback
            if rating <= 2 and specific_mentions > 0 and suggestions:
                score += 0.10
        else:
            reasons.append("rating out of range")
    else:
        score += 0.05  # missing rating is OK but not great

    # 6. Length check — very short feedback is usually low quality
    total_text = (suggestions or "") + (expected or "")
    if len(total_text) > 200:
        score += 0.10
    elif len(total_text) < 50:
        reasons.append("feedback too short")
        score -= 0.05

    score = max(0.0, min(1.0, score))
    rejection_reason = "; ".join(reasons) if score <= 0.5 else None
    return score, rejection_reason


def evaluate_pending_feedback(limit: int = 50) -> dict[str, int]:
    """Run quality gating on pending feedback. Returns counts."""
    _ensure_schema()
    accepted = 0
    rejected = 0

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM ai_feedback WHERE status = 'pending'
                ORDER BY created_at ASC LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()

            for row in rows:
                score, reason = _evaluate_quality(row)
                if score > 0.5:
                    cur.execute(
                        """
                        UPDATE ai_feedback
                        SET status = 'accepted', quality_score = %s, evaluated_at = %s
                        WHERE id = %s
                        """,
                        (score, datetime.now(UTC), row["id"]),
                    )
                    accepted += 1
                else:
                    cur.execute(
                        """
                        UPDATE ai_feedback
                        SET status = 'rejected', quality_score = %s,
                            rejection_reason = %s, evaluated_at = %s
                        WHERE id = %s
                        """,
                        (score, reason, datetime.now(UTC), row["id"]),
                    )
                    rejected += 1
        conn.commit()

    logger.info("Feedback evaluation: %d accepted, %d rejected (of %d pending)", accepted, rejected, len(rows))
    return {"accepted": accepted, "rejected": rejected, "evaluated": len(rows)}


def get_accepted_feedback(limit: int = 100) -> list[dict]:
    """Get accepted feedback that hasn't been consumed by a build yet."""
    _ensure_schema()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT feedback_id, client_id, pr_context, tools_called,
                       results_used, results_expected, quality_rating,
                       improvement_suggestions, quality_score, created_at
                FROM ai_feedback
                WHERE status = 'accepted' AND consumed_at IS NULL
                ORDER BY quality_score DESC, created_at ASC
                LIMIT %s
                """,
                (limit,),
            )
            return cur.fetchall()


def mark_feedback_consumed(feedback_ids: list[str], build_id: str) -> None:
    """Mark feedback as consumed by a specific build."""
    _ensure_schema()
    if not feedback_ids:
        return
    with _connect() as conn:
        with conn.cursor() as cur:
            for fid in feedback_ids:
                cur.execute(
                    """
                    UPDATE ai_feedback
                    SET status = 'consumed', consumed_at = %s, consumed_by_build = %s
                    WHERE feedback_id = %s AND status = 'accepted'
                    """,
                    (datetime.now(UTC), build_id, fid),
                )
        conn.commit()
    logger.info("Marked %d feedback entries as consumed by build %s", len(feedback_ids), build_id)


def get_feedback_stats() -> dict[str, object]:
    _ensure_schema()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) AS n FROM ai_feedback GROUP BY status")
            counts = {row["status"]: row["n"] for row in cur.fetchall()}
            cur.execute(
                """
                SELECT AVG(quality_score) AS avg_score
                FROM ai_feedback WHERE quality_score IS NOT NULL
                """
            )
            avg_row = cur.fetchone()
            cur.execute(
                """
                SELECT AVG(quality_rating) AS avg_rating
                FROM ai_feedback WHERE quality_rating IS NOT NULL
                """
            )
            rating_row = cur.fetchone()
            cur.execute(
                """
                SELECT feedback_id, quality_rating, quality_score,
                       improvement_suggestions, results_expected, created_at
                FROM ai_feedback
                WHERE status = 'accepted' AND consumed_at IS NULL
                ORDER BY quality_score DESC LIMIT 5
                """
            )
            top_pending = cur.fetchall()
    return {
        "pending": counts.get("pending", 0),
        "accepted": counts.get("accepted", 0),
        "rejected": counts.get("rejected", 0),
        "consumed": counts.get("consumed", 0),
        "avg_quality_score": round(float(avg_row["avg_score"] or 0), 4),
        "avg_quality_rating": round(float(rating_row["avg_rating"] or 0), 2),
        "unconsumed_accepted": len(top_pending),
        "top_unconsumed": [
            {
                "feedback_id": r["feedback_id"],
                "quality_rating": r["quality_rating"],
                "quality_score": round(float(r["quality_score"] or 0), 4),
                "improvement_suggestions": (r["improvement_suggestions"] or "")[:200],
                "results_expected": (r["results_expected"] or "")[:200],
            }
            for r in top_pending
        ],
    }


def extract_symbol_signals_from_feedback(feedback: list[dict]) -> dict[str, float]:
    """Extract per-symbol boost/penalty signals from accepted feedback.

    Returns {symbol_id: weight} where weight is in [-1, 1].
    """
    weights: dict[str, list[float]] = {}

    for entry in feedback:
        results_used = entry.get("results_used") or []
        if isinstance(results_used, str):
            results_used = json.loads(results_used)

        rating = entry.get("quality_rating") or 3
        # rating 1-2 = negative (results were bad), 4-5 = positive, 3 = neutral
        signal = (rating - 3) / 2.0  # -1 to +1
        quality_score = entry.get("quality_score") or 0.5
        weighted_signal = signal * quality_score

        for r in results_used:
            if not isinstance(r, dict):
                continue
            symbol_id = r.get("symbol_id")
            if not symbol_id:
                continue
            # If the agent marked a result as "helpful", boost it
            helpful = r.get("helpful")
            if helpful is True:
                weights.setdefault(symbol_id, []).append(weighted_signal + 0.3)
            elif helpful is False:
                weights.setdefault(symbol_id, []).append(weighted_signal - 0.3)
            else:
                weights.setdefault(symbol_id, []).append(weighted_signal)

        # Also check results_expected — symbols that were expected but not found
        expected = entry.get("results_expected") or ""
        if expected and "not found" in expected.lower():
            # This is a signal that the index is missing something
            # We can't boost a specific symbol, but we log it
            pass

    return {sid: sum(vals) / len(vals) for sid, vals in weights.items() if vals}
