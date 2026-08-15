import json
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from app.core.repositories.index_metadata_repository import IndexMetadataRepository
from app.schemas.codebase import CodeChunk, GraphEdge, IndexSnapshot


class PostgresIndexMetadataRepository(IndexMetadataRepository):
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._ensure_schema()

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, row_factory=dict_row)

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS index_snapshots (
                        snapshot_id TEXT PRIMARY KEY,
                        repository_path TEXT NOT NULL,
                        files_indexed INTEGER NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL,
                        is_active BOOLEAN NOT NULL DEFAULT TRUE
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS index_code_chunks (
                        snapshot_id TEXT NOT NULL REFERENCES index_snapshots(snapshot_id) ON DELETE CASCADE,
                        symbol_id TEXT NOT NULL,
                        file_path TEXT NOT NULL,
                        language TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        content TEXT NOT NULL,
                        start_line INTEGER NOT NULL,
                        end_line INTEGER NOT NULL,
                        PRIMARY KEY (snapshot_id, symbol_id, file_path, start_line)
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS index_graph_edges (
                        snapshot_id TEXT NOT NULL REFERENCES index_snapshots(snapshot_id) ON DELETE CASCADE,
                        source_symbol_id TEXT NOT NULL,
                        target_symbol_id TEXT NOT NULL,
                        PRIMARY KEY (snapshot_id, source_symbol_id, target_symbol_id)
                    )
                    """
                )
                cursor.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS index_snapshots_active_idx ON index_snapshots(is_active) WHERE is_active = TRUE"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS index_code_chunks_snapshot_idx ON index_code_chunks(snapshot_id)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS index_graph_edges_snapshot_idx ON index_graph_edges(snapshot_id)"
                )
            connection.commit()

    def reset(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM index_graph_edges")
                cursor.execute("DELETE FROM index_code_chunks")
                cursor.execute("DELETE FROM index_snapshots")
            connection.commit()

    def replace_snapshot(self, snapshot: IndexSnapshot) -> None:
        snapshot_id = uuid4().hex
        created_at = snapshot.created_at or datetime.now(UTC)

        seen_chunks: set[tuple[str, str, int]] = set()
        unique_chunks: list[CodeChunk] = []
        for chunk in snapshot.chunks:
            key = (chunk.symbol_id, chunk.file_path, chunk.start_line)
            if key not in seen_chunks:
                seen_chunks.add(key)
                unique_chunks.append(chunk)

        seen_edges: set[tuple[str, str]] = set()
        unique_edges: list[GraphEdge] = []
        for edge in snapshot.graph_edges:
            key = (edge.source_symbol_id, edge.target_symbol_id)
            if key not in seen_edges:
                seen_edges.add(key)
                unique_edges.append(edge)

        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE index_snapshots SET is_active = FALSE WHERE is_active = TRUE")
                cursor.execute(
                    """
                    INSERT INTO index_snapshots (snapshot_id, repository_path, files_indexed, created_at, is_active, last_indexed_commit)
                    VALUES (%s, %s, %s, %s, TRUE, %s)
                    """,
                    (snapshot_id, snapshot.repository_path, snapshot.files_indexed, created_at, snapshot.last_indexed_commit),
                )
                cursor.executemany(
                    """
                    INSERT INTO index_code_chunks (
                        snapshot_id, symbol_id, file_path, language, kind, content, start_line, end_line
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            snapshot_id,
                            chunk.symbol_id,
                            chunk.file_path,
                            chunk.language,
                            chunk.kind,
                            chunk.content,
                            chunk.start_line,
                            chunk.end_line,
                        )
                        for chunk in unique_chunks
                    ],
                )
                cursor.executemany(
                    """
                    INSERT INTO index_graph_edges (snapshot_id, source_symbol_id, target_symbol_id)
                    VALUES (%s, %s, %s)
                    """,
                    [
                        (snapshot_id, edge.source_symbol_id, edge.target_symbol_id)
                        for edge in unique_edges
                    ],
                )
            connection.commit()

    def load_snapshot(self) -> IndexSnapshot | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT snapshot_id, repository_path, files_indexed, created_at, last_indexed_commit
                    FROM index_snapshots
                    WHERE is_active = TRUE
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                )
                snapshot_row = cursor.fetchone()
                if snapshot_row is None:
                    return None

                cursor.execute(
                    """
                    SELECT symbol_id, file_path, language, kind, content, start_line, end_line
                    FROM index_code_chunks
                    WHERE snapshot_id = %s
                    ORDER BY file_path, start_line, symbol_id
                    """,
                    (snapshot_row["snapshot_id"],),
                )
                chunk_rows = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT source_symbol_id, target_symbol_id
                    FROM index_graph_edges
                    WHERE snapshot_id = %s
                    ORDER BY source_symbol_id, target_symbol_id
                    """,
                    (snapshot_row["snapshot_id"],),
                )
                edge_rows = cursor.fetchall()

        return IndexSnapshot(
            repository_path=snapshot_row["repository_path"],
            files_indexed=int(snapshot_row["files_indexed"]),
            created_at=snapshot_row["created_at"],
            last_indexed_commit=snapshot_row["last_indexed_commit"] or "",
            chunks=[CodeChunk.model_validate(row) for row in chunk_rows],
            graph_edges=[GraphEdge.model_validate(row) for row in edge_rows],
        )

    def update_incremental(
        self,
        modified_chunks: list[CodeChunk],
        removed_symbol_ids: list[str],
        modified_edges: list[GraphEdge],
        removed_edge_keys: list[tuple[str, str]],
        last_indexed_commit: str = "",
    ) -> None:
        """Update the active snapshot in-place without full re-insert."""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT snapshot_id FROM index_snapshots WHERE is_active = TRUE LIMIT 1"
                )
                row = cursor.fetchone()
                if row is None:
                    return
                snapshot_id = row["snapshot_id"]

                if removed_symbol_ids:
                    cursor.executemany(
                        "DELETE FROM index_code_chunks WHERE snapshot_id = %s AND symbol_id = %s",
                        [(snapshot_id, sid) for sid in removed_symbol_ids],
                    )

                if removed_edge_keys:
                    cursor.executemany(
                        "DELETE FROM index_graph_edges WHERE snapshot_id = %s AND source_symbol_id = %s AND target_symbol_id = %s",
                        [(snapshot_id, s, t) for s, t in removed_edge_keys],
                    )

                if modified_chunks:
                    seen: set[tuple[str, str, int]] = set()
                    unique: list[CodeChunk] = []
                    for chunk in modified_chunks:
                        key = (chunk.symbol_id, chunk.file_path, chunk.start_line)
                        if key not in seen:
                            seen.add(key)
                            unique.append(chunk)

                    cursor.executemany(
                        """
                        DELETE FROM index_code_chunks WHERE snapshot_id = %s AND symbol_id = %s AND file_path = %s
                        """,
                        [(snapshot_id, c.symbol_id, c.file_path) for c in unique],
                    )
                    cursor.executemany(
                        """
                        INSERT INTO index_code_chunks (
                            snapshot_id, symbol_id, file_path, language, kind, content, start_line, end_line
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        [
                            (
                                snapshot_id,
                                chunk.symbol_id,
                                chunk.file_path,
                                chunk.language,
                                chunk.kind,
                                chunk.content,
                                chunk.start_line,
                                chunk.end_line,
                            )
                            for chunk in unique
                        ],
                    )

                if modified_edges:
                    seen_e: set[tuple[str, str]] = set()
                    unique_e: list[GraphEdge] = []
                    for edge in modified_edges:
                        key = (edge.source_symbol_id, edge.target_symbol_id)
                        if key not in seen_e:
                            seen_e.add(key)
                            unique_e.append(edge)

                    cursor.executemany(
                        """
                        DELETE FROM index_graph_edges WHERE snapshot_id = %s AND source_symbol_id = %s AND target_symbol_id = %s
                        """,
                        [(snapshot_id, e.source_symbol_id, e.target_symbol_id) for e in unique_e],
                    )
                    cursor.executemany(
                        """
                        INSERT INTO index_graph_edges (snapshot_id, source_symbol_id, target_symbol_id)
                        VALUES (%s, %s, %s)
                        """,
                        [
                            (snapshot_id, edge.source_symbol_id, edge.target_symbol_id)
                            for edge in unique_e
                        ],
                    )

                if last_indexed_commit:
                    cursor.execute(
                        "UPDATE index_snapshots SET last_indexed_commit = %s WHERE snapshot_id = %s",
                        (last_indexed_commit, snapshot_id),
                    )
            connection.commit()