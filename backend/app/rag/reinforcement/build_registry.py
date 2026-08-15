"""Build registry — versioned semantic/graph builds with rollback.

Every build (semantic, graph, or both) is registered here with a unique
build_id. Builds form a parent chain so we can:
  1. Track which build replaced which
  2. Evaluate build quality after feedback accumulates
  3. Rollback to a previous build if the new one is worse
  4. Re-trigger the parent build as a new build (idempotency)

A build's quality score is computed from accepted AI feedback that was
consumed by that build. If the score drops below the parent's score,
the reinforcement agent triggers a rollback.
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
                    CREATE TABLE IF NOT EXISTS build_registry (
                        build_id          TEXT PRIMARY KEY,
                        parent_build_id   TEXT,
                        build_type        TEXT NOT NULL,
                        status            TEXT NOT NULL DEFAULT 'building',
                        weaviate_collection TEXT,
                        neo4j_gen         INT,
                        chunk_count       INT DEFAULT 0,
                        graph_nodes       INT DEFAULT 0,
                        graph_edges       INT DEFAULT 0,
                        feedback_ids      JSONB NOT NULL DEFAULT '[]',
                        feedback_count    INT DEFAULT 0,
                        quality_score     DOUBLE PRECISION,
                        parent_quality_score DOUBLE PRECISION,
                        created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        completed_at      TIMESTAMPTZ,
                        superseded_at     TIMESTAMPTZ,
                        rolled_back_at    TIMESTAMPTZ,
                        rollback_reason   TEXT,
                        metadata          JSONB NOT NULL DEFAULT '{}'
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS br_status_idx ON build_registry(status)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS br_parent_idx ON build_registry(parent_build_id)"
                )
            conn.commit()
        _schema_ready = True
        logger.info("Build registry schema ready")


def register_build(
    build_type: str,
    parent_build_id: str | None = None,
    weaviate_collection: str | None = None,
    neo4j_gen: int | None = None,
    feedback_ids: list[str] | None = None,
    metadata: dict | None = None,
) -> str:
    """Register a new build. Returns the build_id."""
    _ensure_schema()
    build_id = uuid.uuid4().hex[:16]
    parent_quality = None

    if parent_build_id:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT quality_score FROM build_registry WHERE build_id = %s",
                    (parent_build_id,),
                )
                row = cur.fetchone()
                if row and row["quality_score"] is not None:
                    parent_quality = float(row["quality_score"])

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO build_registry (
                    build_id, parent_build_id, build_type, status,
                    weaviate_collection, neo4j_gen,
                    feedback_ids, feedback_count,
                    parent_quality_score, created_at, metadata
                ) VALUES (%s, %s, %s, 'building', %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    build_id,
                    parent_build_id,
                    build_type,
                    weaviate_collection,
                    neo4j_gen,
                    json.dumps(feedback_ids or []),
                    len(feedback_ids or []),
                    parent_quality,
                    datetime.now(UTC),
                    json.dumps(metadata or {}),
                ),
            )
        conn.commit()

    logger.info(
        "Registered build %s (type=%s parent=%s weaviate=%s neo4j_gen=%s)",
        build_id, build_type, parent_build_id, weaviate_collection, neo4j_gen,
    )
    return build_id


def complete_build(
    build_id: str,
    chunk_count: int = 0,
    graph_nodes: int = 0,
    graph_edges: int = 0,
) -> None:
    """Mark a build as completed and active."""
    _ensure_schema()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE build_registry SET
                    status = 'active', completed_at = %s,
                    chunk_count = %s, graph_nodes = %s, graph_edges = %s
                WHERE build_id = %s
                """,
                (datetime.now(UTC), chunk_count, graph_nodes, graph_edges, build_id),
            )
            cur.execute(
                """
                UPDATE build_registry SET status = 'superseded', superseded_at = %s
                WHERE status = 'active' AND build_id != %s
                """,
                (datetime.now(UTC), build_id),
            )
        conn.commit()
    logger.info("Build %s completed and marked active", build_id)


def get_active_build() -> dict | None:
    """Get the current active build."""
    _ensure_schema()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM build_registry WHERE status = 'active'
                ORDER BY completed_at DESC LIMIT 1
                """
            )
            row = cur.fetchone()
    return dict(row) if row else None


def get_build(build_id: str) -> dict | None:
    _ensure_schema()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM build_registry WHERE build_id = %s",
                (build_id,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def update_quality_score(build_id: str, score: float) -> None:
    """Update a build's quality score based on feedback."""
    _ensure_schema()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE build_registry SET quality_score = %s WHERE build_id = %s",
                (score, build_id),
            )
        conn.commit()
    logger.info("Build %s quality score updated to %.4f", build_id, score)


def rollback_build(build_id: str, reason: str) -> dict | None:
    """Rollback a build — mark it as rolled_back and return the parent build info."""
    _ensure_schema()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM build_registry WHERE build_id = %s",
                (build_id,),
            )
            build = cur.fetchone()
            if not build:
                return None
            if build["status"] not in ("active", "superseded"):
                return None

            cur.execute(
                """
                UPDATE build_registry SET
                    status = 'rolled_back', rolled_back_at = %s, rollback_reason = %s
                WHERE build_id = %s
                """,
                (datetime.now(UTC), reason, build_id),
            )

            parent_id = build.get("parent_build_id")
            parent = None
            if parent_id:
                cur.execute(
                    "SELECT * FROM build_registry WHERE build_id = %s",
                    (parent_id,),
                )
                parent = cur.fetchone()
                if parent and parent["status"] in ("superseded", "rolled_back"):
                    cur.execute(
                        """
                        UPDATE build_registry SET
                            status = 'active', superseded_at = NULL, rolled_back_at = NULL
                        WHERE build_id = %s
                        """,
                        (parent_id,),
                    )
        conn.commit()

    logger.warning("Build %s rolled back: %s — restoring parent %s", build_id, reason, parent_id)
    if parent:
        return dict(parent)
    return None


def get_build_history(limit: int = 20) -> list[dict]:
    _ensure_schema()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT build_id, parent_build_id, build_type, status,
                       chunk_count, graph_nodes, graph_edges,
                       feedback_count, quality_score, parent_quality_score,
                       created_at, completed_at, rollback_reason
                FROM build_registry
                ORDER BY created_at DESC LIMIT %s
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]


def get_build_stats() -> dict[str, object]:
    _ensure_schema()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) AS n FROM build_registry GROUP BY status")
            counts = {row["status"]: row["n"] for row in cur.fetchall()}
            cur.execute("SELECT * FROM build_registry WHERE status = 'active' LIMIT 1")
            active = cur.fetchone()
    return {
        "total_builds": sum(counts.values()),
        "active": counts.get("active", 0),
        "superseded": counts.get("superseded", 0),
        "rolled_back": counts.get("rolled_back", 0),
        "building": counts.get("building", 0),
        "active_build": dict(active) if active else None,
    }
