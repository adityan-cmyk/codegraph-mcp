import threading

from app.core.config import settings
from app.schemas.codebase import GraphNeighborhood


class InMemoryGraphIndex:
    def __init__(self) -> None:
        self._upstream: dict[str, set[str]] = {}
        self._downstream: dict[str, set[str]] = {}
        self._used_by: dict[str, set[str]] = {}
        self._uses: dict[str, set[str]] = {}
        self._metadata: dict[str, dict] = {}

    def reset(self) -> None:
        self._upstream.clear()
        self._downstream.clear()
        self._used_by.clear()
        self._uses.clear()
        self._metadata.clear()

    def reset_documents(self) -> None:
        self.reset()

    def upsert_symbol(self, symbol_id: str, *, calls: list[str] | None = None, called_by: list[str] | None = None, uses: list[str] | None = None, used_by: list[str] | None = None, uses_with_modes: list[tuple[str, list[str]]] | None = None, metadata: dict | None = None) -> None:
        self._downstream.setdefault(symbol_id, set()).update(calls or [])
        self._upstream.setdefault(symbol_id, set()).update(called_by or [])
        if uses_with_modes:
            for target, _modes in uses_with_modes:
                self._uses.setdefault(symbol_id, set()).add(target)
                self._used_by.setdefault(target, set()).add(symbol_id)
        else:
            self._uses.setdefault(symbol_id, set()).update(uses or [])
            for target in uses or []:
                self._used_by.setdefault(target, set()).add(symbol_id)
        self._used_by.setdefault(symbol_id, set()).update(used_by or [])
        if metadata:
            self._metadata[symbol_id] = metadata

    def get_blast_radius(self, symbol_id: str) -> GraphNeighborhood:
        return GraphNeighborhood(
            symbol_id=symbol_id,
            upstream=sorted(self._upstream.get(symbol_id, set())),
            downstream=sorted(self._downstream.get(symbol_id, set())),
            used_by=sorted(self._used_by.get(symbol_id, set())),
            uses=sorted(self._uses.get(symbol_id, set())),
        )

    def get_neighbors(self, symbol_id: str, depth: int = 1) -> GraphNeighborhood:
        return self.get_blast_radius(symbol_id)

    def get_stats(self) -> dict[str, int]:
        node_ids = set(self._upstream) | set(self._downstream) | set(self._used_by) | set(self._uses)
        edge_count = (
            sum(len(targets) for targets in self._downstream.values()) +
            sum(len(targets) for targets in self._uses.values())
        )
        return {
            "graph_nodes": len(node_ids),
            "graph_edges": edge_count,
        }

    def has_symbol(self, symbol_id: str) -> bool:
        return symbol_id in self._upstream or symbol_id in self._downstream or symbol_id in self._used_by or symbol_id in self._uses

    def search_symbols(self, query: str, limit: int = 20) -> list[dict[str, object]]:
        query_lower = query.lower()
        node_ids = set(self._upstream) | set(self._downstream) | set(self._used_by) | set(self._uses)
        results: list[dict[str, object]] = []
        for sid in node_ids:
            if query_lower in sid.lower():
                meta = self._metadata.get(sid, {})
                results.append({
                    "symbol_id": sid,
                    "short_name": sid.split("::")[-1],
                    "kind": meta.get("kind"),
                    "file_path": meta.get("file_path"),
                    "start_line": meta.get("start_line"),
                    "end_line": meta.get("end_line"),
                    "module": meta.get("module"),
                    "has_calls": bool(self._downstream.get(sid)),
                    "has_uses": bool(self._uses.get(sid)),
                    "has_callers": bool(self._upstream.get(sid)),
                    "has_users": bool(self._used_by.get(sid)),
                })
                if len(results) >= limit:
                    break
        return results

    def traverse(self, symbol_id: str, depth: int = 1) -> list[GraphNeighborhood]:
        if depth < 1:
            raise ValueError("Depth must be at least 1.")

        visited: set[str] = set()
        frontier = [symbol_id]
        neighborhoods: list[GraphNeighborhood] = []

        for _ in range(depth):
            next_frontier: list[str] = []
            for current in frontier:
                if current in visited:
                    continue
                visited.add(current)
                neighborhood = self.get_blast_radius(current)
                neighborhoods.append(neighborhood)
                for adjacent in neighborhood.upstream + neighborhood.downstream + neighborhood.used_by + neighborhood.uses:
                    if adjacent not in visited:
                        next_frontier.append(adjacent)
            frontier = next_frontier
            if not frontier:
                break

        return neighborhoods

    def cleanup_other_gens(self) -> None:
        pass

    def close(self) -> None:
        pass


class GraphIndexProxy:
    """Thread-safe proxy that delegates to an active graph index backend.
    Supports atomic swap so a rebuild can construct a new graph in the
    background while the old one continues serving queries. Also keeps
    a history of previous backends for rollback."""

    def __init__(self, backend) -> None:
        self._lock = threading.Lock()
        self._backend = backend
        self._history: list = []  # list of previous backends for rollback

    def swap(self, new_backend, build_id: str | None = None):
        """Atomically replace the active backend. Returns the old backend.
        The old backend is kept in history for potential rollback."""
        with self._lock:
            old = self._backend
            self._history.append(old)
            self._backend = new_backend
        return old

    def rollback(self):
        """Rollback to the previous backend. Returns (old, current) or None."""
        with self._lock:
            if not self._history:
                return None
            current = self._backend
            old = self._history.pop()
            self._backend = old
        return old, current

    def _active(self):
        with self._lock:
            return self._backend

    def reset(self) -> None:
        self._active().reset()

    def upsert_symbol(self, *args, **kwargs) -> None:
        self._active().upsert_symbol(*args, **kwargs)

    def get_blast_radius(self, symbol_id: str) -> GraphNeighborhood:
        return self._active().get_blast_radius(symbol_id)

    def get_neighbors(self, symbol_id: str, depth: int = 1) -> GraphNeighborhood:
        backend = self._active()
        if hasattr(backend, "get_neighbors"):
            return backend.get_neighbors(symbol_id, depth=depth)
        return backend.get_blast_radius(symbol_id)

    def traverse(self, symbol_id: str, depth: int = 2) -> list[GraphNeighborhood]:
        return self._active().traverse(symbol_id, depth=depth)

    def get_stats(self) -> dict[str, int]:
        return self._active().get_stats()

    def has_symbol(self, symbol_id: str) -> bool:
        return self._active().has_symbol(symbol_id)

    def search_symbols(self, query: str, limit: int = 20) -> list[dict[str, object]]:
        return self._active().search_symbols(query, limit=limit)

    def close(self) -> None:
        backend = self._active()
        if hasattr(backend, "close"):
            backend.close()


def _build_graph_index(gen: int = 0):
    if settings.graph_index_backend == "neo4j":
        from app.rag.retrieval.neo4j_graph import Neo4jGraphIndex
        return Neo4jGraphIndex(
            uri=settings.neo4j_uri,
            username=settings.neo4j_username,
            password=settings.neo4j_password,
            gen=gen,
        )
    return InMemoryGraphIndex()


def _get_current_gen() -> int:
    """Query Neo4j for the highest existing generation number."""
    if settings.graph_index_backend != "neo4j":
        return 0
    try:
        from app.rag.retrieval.neo4j_graph import Neo4jGraphIndex
        probe = Neo4jGraphIndex(
            uri=settings.neo4j_uri,
            username=settings.neo4j_username,
            password=settings.neo4j_password,
            gen=0,
        )
        driver = probe._get_driver()
        with driver.session() as session:
            result = session.run("MATCH (n:Symbol) RETURN max(n.gen) AS max_gen")
            record = result.single()
            max_gen = record["max_gen"] if record and record["max_gen"] is not None else 0
        return max_gen
    except Exception:
        return 0


graph_index = GraphIndexProxy(_build_graph_index(gen=_get_current_gen()))


def get_blast_radius(symbol_id: str) -> dict[str, list[str]]:
    neighborhood = graph_index.get_blast_radius(symbol_id)
    return neighborhood.model_dump()
