"""Postgres-backed feedback store for reinforcement learning.

Stores per-query, per-symbol feedback from agents and computes
boost/penalty weights that are applied during semantic search reranking.
"""

import logging
import threading
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
                    CREATE TABLE IF NOT EXISTS search_feedback (
                        id           SERIAL PRIMARY KEY,
                        query_text   TEXT NOT NULL,
                        symbol_id    TEXT NOT NULL,
                        original_score DOUBLE PRECISION NOT NULL,
                        feedback     SMALLINT NOT NULL,
                        reason       TEXT,
                        created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS sf_symbol_idx ON search_feedback(symbol_id)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS sf_query_idx ON search_feedback(query_text)"
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS symbol_reinforcement (
                        symbol_id      TEXT PRIMARY KEY,
                        positive_count INT DEFAULT 0,
                        negative_count INT DEFAULT 0,
                        boost_weight   DOUBLE PRECISION DEFAULT 0.0,
                        last_updated   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS query_expansion (
                        query_text      TEXT NOT NULL,
                        expansion_term  TEXT NOT NULL,
                        weight          DOUBLE PRECISION DEFAULT 1.0,
                        positive_count  INT DEFAULT 0,
                        last_seen       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (query_text, expansion_term)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS query_patterns (
                        id              SERIAL PRIMARY KEY,
                        query_text      TEXT NOT NULL,
                        symbol_id       TEXT NOT NULL,
                        was_helpful     BOOLEAN,
                        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS qp_query_idx ON query_patterns(query_text)"
                )
            conn.commit()
        _schema_ready = True
        logger.info("Reinforcement feedback schema ready")


def record_feedback(
    query_text: str,
    symbol_id: str,
    original_score: float,
    feedback: int,
    reason: str | None = None,
) -> None:
    """Store a single feedback signal (+1 positive, -1 negative)."""
    _ensure_schema()
    fb = 1 if feedback > 0 else -1
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO search_feedback (query_text, symbol_id, original_score, feedback, reason)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (query_text, symbol_id, original_score, fb, reason),
            )
            cur.execute(
                """
                INSERT INTO symbol_reinforcement (symbol_id, positive_count, negative_count, boost_weight, last_updated)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (symbol_id) DO UPDATE SET
                    positive_count = symbol_reinforcement.positive_count + EXCLUDED.positive_count,
                    negative_count = symbol_reinforcement.negative_count + EXCLUDED.negative_count,
                    boost_weight = (
                        CASE
                            WHEN (symbol_reinforcement.positive_count + EXCLUDED.positive_count
                                  + symbol_reinforcement.negative_count + EXCLUDED.negative_count) = 0
                            THEN 0.0
                            ELSE (symbol_reinforcement.positive_count + EXCLUDED.positive_count)::FLOAT
                                 / (symbol_reinforcement.positive_count + EXCLUDED.positive_count
                                    + symbol_reinforcement.negative_count + EXCLUDED.negative_count + 5)
                        END
                    ) * 2.0 - 1.0,
                    last_updated = %s
                """,
                (
                    symbol_id,
                    1 if fb > 0 else 0,
                    0 if fb > 0 else 1,
                    0.0,
                    datetime.now(UTC),
                    datetime.now(UTC),
                ),
            )

            if fb > 0:
                short_name = symbol_id.split("::")[-1]
                cur.execute(
                    """
                    INSERT INTO query_expansion (query_text, expansion_term, weight, positive_count, last_seen)
                    VALUES (%s, %s, 1.0, 1, %s)
                    ON CONFLICT (query_text, expansion_term) DO UPDATE SET
                        positive_count = query_expansion.positive_count + 1,
                        weight = LEAST(query_expansion.weight + 0.1, 3.0),
                        last_seen = EXCLUDED.last_seen
                    """,
                    (query_text, short_name, datetime.now(UTC)),
                )
        conn.commit()
    logger.info("Feedback recorded: query=%r symbol=%s feedback=%+d", query_text[:60], symbol_id, fb)


def get_boost_weights() -> dict[str, float]:
    """Return {symbol_id: boost_weight} for all symbols with feedback."""
    _ensure_schema()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol_id, boost_weight
                FROM symbol_reinforcement
                WHERE boost_weight != 0.0
                """
            )
            return {row["symbol_id"]: float(row["boost_weight"]) for row in cur.fetchall()}


def get_query_expansions(query_text: str) -> list[tuple[str, float]]:
    """Return [(expansion_term, weight), ...] for a given query."""
    _ensure_schema()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT expansion_term, weight
                FROM query_expansion
                WHERE query_text = %s AND weight > 1.0
                ORDER BY weight DESC
                LIMIT 5
                """,
                (query_text,),
            )
            return [(row["expansion_term"], float(row["weight"])) for row in cur.fetchall()]


def get_reinforcement_stats() -> dict[str, object]:
    _ensure_schema()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM search_feedback")
            total = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM symbol_reinforcement WHERE boost_weight > 0")
            boosted = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM symbol_reinforcement WHERE boost_weight < 0")
            penalized = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM query_expansion")
            expansions = cur.fetchone()["n"]
            cur.execute(
                """
                SELECT symbol_id, boost_weight, positive_count, negative_count
                FROM symbol_reinforcement
                WHERE boost_weight != 0.0
                ORDER BY ABS(boost_weight) DESC
                LIMIT 10
                """
            )
            top = cur.fetchall()
    return {
        "total_feedback": total,
        "boosted_symbols": boosted,
        "penalized_symbols": penalized,
        "query_expansions": expansions,
        "top_adjusted_symbols": [
            {
                "symbol_id": r["symbol_id"],
                "boost_weight": round(float(r["boost_weight"]), 4),
                "positive": r["positive_count"],
                "negative": r["negative_count"],
            }
            for r in top
        ],
    }


def record_query_pattern(query_text: str, helpful_symbols: list[str]) -> None:
    """Record which symbols were helpful for a given query (Idea 9)."""
    _ensure_schema()
    if not helpful_symbols:
        return
    with _connect() as conn:
        with conn.cursor() as cur:
            for sid in helpful_symbols:
                cur.execute(
                    """
                    INSERT INTO query_patterns (query_text, symbol_id, was_helpful)
                    VALUES (%s, %s, TRUE)
                    """,
                    (query_text, sid),
                )
        conn.commit()


def get_similar_query_boosts(query_text: str, limit: int = 5) -> dict[str, float]:
    """Find symbols that were helpful for similar past queries (Idea 9).

    Returns {symbol_id: boost_weight} for symbols that past agents
    found helpful for queries similar to the given one.
    """
    _ensure_schema()
    query_lower = query_text.lower()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT symbol_id, COUNT(*) AS freq
                FROM query_patterns
                WHERE was_helpful = TRUE
                  AND (LOWER(query_text) = %s OR LOWER(query_text) LIKE %s)
                GROUP BY symbol_id
                ORDER BY freq DESC
                LIMIT %s
                """,
                (query_lower, f"%{query_lower[:40]}%", limit),
            )
            rows = cur.fetchall()
    return {row["symbol_id"]: min(0.1 * row["freq"], 0.3) for row in rows}
