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
        removed_symbol_ids: list[str],
        modified_edges: list[GraphEdge],
        removed_edge_keys: list[tuple[str, str]],
        last_indexed_commit: str = "",
    ) -> None:
        with self._lock:
            if self._snapshot is None:
                return
            removed_set = set(removed_symbol_ids)
            self._snapshot.chunks = [
                c for c in self._snapshot.chunks if c.symbol_id not in removed_set
            ] + modified_chunks
            removed_edge_set = set(removed_edge_keys)
            self._snapshot.graph_edges = [
                e for e in self._snapshot.graph_edges
                if (e.source_symbol_id, e.target_symbol_id) not in removed_edge_set
            ] + modified_edges
            if last_indexed_commit:
                self._snapshot = self._snapshot.model_copy(
                    update={"last_indexed_commit": last_indexed_commit}
                )