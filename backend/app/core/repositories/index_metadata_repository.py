from abc import ABC, abstractmethod

from app.schemas.codebase import CodeChunk, GraphEdge, IndexSnapshot


class IndexMetadataRepository(ABC):
    @abstractmethod
    def reset(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def replace_snapshot(self, snapshot: IndexSnapshot) -> None:
        raise NotImplementedError

    @abstractmethod
    def load_snapshot(self) -> IndexSnapshot | None:
        raise NotImplementedError

    def update_incremental(
        self,
        modified_chunks: list[CodeChunk],
        removed_symbols: list[tuple[str, str]],
        modified_edges: list[GraphEdge],
        removed_edge_keys: list[tuple[str, str]],
        last_indexed_commit: str = "",
        symbol_set_hash: str = "",
    ) -> None:
        raise NotImplementedError