from threading import Lock

from app.core.repositories.index_metadata_repository import IndexMetadataRepository
from app.schemas.codebase import CodeChunk, GraphEdge, IndexSnapshot


class InMemoryIndexMetadataRepository(IndexMetadataRepository):
    def __init__(self) -> None:
        self._snapshot: IndexSnapshot | None = None
        self._lock = Lock()

    def reset(self) -> None:
        with self._lock:
            self._snapshot = None

    def replace_snapshot(self, snapshot: IndexSnapshot) -> None:
        with self._lock:
            self._snapshot = snapshot

    def load_snapshot(self) -> IndexSnapshot | None:
        with self._lock:
            return self._snapshot

    def update_incremental(
        self,
        modified_chunks: list[CodeChunk],
        removed_symbols: list[tuple[str, str]],
        modified_edges: list[GraphEdge],
        removed_edge_keys: list[tuple[str, str]],
        last_indexed_commit: str = "",
        symbol_set_hash: str = "",
    ) -> None:
        with self._lock:
            if self._snapshot is None:
                return
            # Mirror the Postgres contract: modified rows replace themselves
            # by (symbol_id, file_path) / (source, target) delete-before-insert.
            # Append-only here produced duplicate rows on every re-extraction
            # (4 chunks + 3 re-extracted = 7) while Postgres stayed at 5.
            removed_set = set(removed_symbols)
            modified_pairs = {(c.symbol_id, c.file_path) for c in modified_chunks}
            seen: set[tuple[str, str, int]] = set()
            unique_chunks: list[CodeChunk] = []
            for chunk in modified_chunks:
                key = (chunk.symbol_id, chunk.file_path, chunk.start_line)
                if key not in seen:
                    seen.add(key)
                    unique_chunks.append(chunk)
            self._snapshot.chunks = [
                c for c in self._snapshot.chunks
                if (c.symbol_id, c.file_path) not in removed_set
                and (c.symbol_id, c.file_path) not in modified_pairs
            ] + unique_chunks
            removed_edge_set = set(removed_edge_keys)
            modified_edge_pairs = {(e.source_symbol_id, e.target_symbol_id) for e in modified_edges}
            seen_e: set[tuple[str, str]] = set()
            unique_edges: list[GraphEdge] = []
            for edge in modified_edges:
                key = (edge.source_symbol_id, edge.target_symbol_id)
                if key not in seen_e:
                    seen_e.add(key)
                    unique_edges.append(edge)
            self._snapshot.graph_edges = [
                e for e in self._snapshot.graph_edges
                if (e.source_symbol_id, e.target_symbol_id) not in removed_edge_set
                and (e.source_symbol_id, e.target_symbol_id) not in modified_edge_pairs
            ] + unique_edges
            if last_indexed_commit or symbol_set_hash:
                from datetime import UTC, datetime

                self._snapshot = self._snapshot.model_copy(
                    update={
                        "last_indexed_commit": last_indexed_commit or self._snapshot.last_indexed_commit,
                        "symbol_set_hash": symbol_set_hash or self._snapshot.symbol_set_hash,
                        # incremental writes stamp time too — a frozen
                        # created_at hid mutating updates behind a stale label
                        "created_at": datetime.now(UTC),
                    }
                )