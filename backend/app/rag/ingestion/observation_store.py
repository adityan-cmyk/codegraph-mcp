"""Postgres persistence for code observations (the metal-detector store).

Observations are extracted from source files during indexing and keyed by
file (replace-on-sync keeps re-indexing idempotent) with a symbol_id attached
for blast-radius queries.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg
from psycopg.rows import dict_row

from app.core.config import settings
from app.rag.ingestion.observations import Observation, extract_observations
from app.schemas.codebase import CodeChunk

logger = logging.getLogger(__name__)

_DSN = settings.postgres_dsn
_schema_lock = threading.Lock()
_schema_ready = False

KINDS = ("todo", "fixme", "hack", "xxx", "commented_code", "error_swallow", "fail_open", "stub_fn", "unsafe_block", "ffi_boundary", "panic_path", "unwrap_density")


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
                    CREATE TABLE IF NOT EXISTS code_observations (
                        id BIGSERIAL PRIMARY KEY,
                        file_path TEXT NOT NULL,
                        line INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        detail TEXT,
                        symbol_id TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cur.execute("CREATE INDEX IF NOT EXISTS code_observations_symbol ON code_observations(symbol_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS code_observations_file ON code_observations(file_path)")
                cur.execute("CREATE INDEX IF NOT EXISTS code_observations_kind ON code_observations(kind)")
            conn.commit()
        _schema_ready = True


def replace_file_observations(file_path: str, observations: list[Observation]) -> None:
    """Idempotent per-file sync: wipe the file's rows, insert fresh."""
    _ensure_schema()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM code_observations WHERE file_path = %s", (file_path,))
            cur.executemany(
                """
                INSERT INTO code_observations (file_path, line, kind, detail, symbol_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
                [
                    (o.file_path, o.line, o.kind, o.detail, o.symbol_id)
                    for o in observations
                ],
            )
        conn.commit()


def get_observations_for_symbols(symbol_ids: list[str], kinds: list[str] | None = None) -> list[dict]:
    _ensure_schema()
    if not symbol_ids:
        return []
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol_id, file_path, line, kind, detail
                FROM code_observations
                WHERE symbol_id = ANY(%s)
                  AND (%s::text[] IS NULL OR kind = ANY(%s))
                ORDER BY kind, file_path, line
                """,
                (symbol_ids, kinds, kinds),
            )
            return cur.fetchall()


def get_observation_stats() -> dict[str, int]:
    _ensure_schema()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT kind, COUNT(*) AS n FROM code_observations GROUP BY kind")
            return {r["kind"]: r["n"] for r in cur.fetchall()}


def sync_observations(repository_path, chunks: list[CodeChunk], files: list[str] | None = None,
                      max_workers: int = 8) -> dict[str, int]:
    """Extract + persist observations for the given files (default: all files
    present in the chunk snapshot). Safe to re-run — per-file replace."""
    from pathlib import Path

    root = Path(repository_path)
    by_file: dict[str, list[CodeChunk]] = {}
    for c in chunks:
        by_file.setdefault(c.file_path, []).append(c)

    targets = files if files is not None else sorted(by_file.keys())
    stats: dict[str, int] = {}

    def _do(rel: str) -> int:
        path = root / rel
        try:
            source = path.read_text(errors="replace")
        except OSError:
            return 0
        obs = extract_observations(source, rel, by_file.get(rel, []))
        replace_file_observations(rel, obs)
        return len(obs)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        counts = list(pool.map(_do, targets))
    stats["files"] = len(targets)
    stats["observations"] = sum(counts)
    logger.info("Observation sync: %d files, %d observations", stats["files"], stats["observations"])
    return stats
