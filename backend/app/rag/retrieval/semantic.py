from app.core.config import settings
from app.rag.embeddings import cosine_similarity, embed_text
from app.schemas.codebase import CodeChunk, SemanticMatch
from app.schemas.incident import IncidentFingerprint, ResolutionPackage


class InMemorySemanticIndex:
    def __init__(self) -> None:
        self._documents: list[tuple[CodeChunk, list[float]]] = []
        self._resolved_errors: list[tuple[str, dict[str, object], list[float]]] = []

    def reset(self) -> None:
        self._documents.clear()
        self._resolved_errors.clear()

    def reset_documents(self) -> None:
        self._documents.clear()

    def upsert_chunks(self, chunks: list[CodeChunk]) -> None:
        for chunk in chunks:
            self._documents = [item for item in self._documents if item[0].symbol_id != chunk.symbol_id]
            self._documents.append((chunk, embed_text(chunk.content)))

    def add_resolved_error(self, fingerprint: IncidentFingerprint, resolution: dict[str, object]) -> None:
        signature = " ".join(
            [fingerprint.service, fingerprint.panic_type, fingerprint.top_frame, fingerprint.commit_hash]
        )
        self._resolved_errors.append((fingerprint.service, resolution, embed_text(signature)))

    def add_resolution_package(self, package: ResolutionPackage) -> None:
        self.add_resolved_error(
            package.fingerprint,
            {"root_cause": package.root_cause, "patch": package.patch},
        )

    def get_stats(self) -> dict[str, int]:
        return {
            "semantic_documents": len(self._documents),
            "resolved_error_documents": len(self._resolved_errors),
        }

    def has_symbol(self, symbol_id: str) -> bool:
        return any(chunk.symbol_id == symbol_id for chunk, _ in self._documents)

    def get_chunk(self, symbol_id: str) -> CodeChunk | None:
        for chunk, _ in self._documents:
            if chunk.symbol_id == symbol_id:
                return chunk
        return None

    def query_chunks(self, query: str, *, limit: int = 5) -> list[SemanticMatch]:
        query_embedding = embed_text(query)
        scored = []
        for chunk, embedding in self._documents:
            score = cosine_similarity(query_embedding, embedding)
            scored.append(
                SemanticMatch(
                    symbol_id=chunk.symbol_id,
                    score=score,
                    content=chunk.content,
                    source=chunk.file_path,
                )
            )
        return sorted(scored, key=lambda item: item.score, reverse=True)[:limit]

    def lookup_resolved_error(self, fingerprint: IncidentFingerprint) -> dict[str, object]:
        signature = " ".join(
            [fingerprint.service, fingerprint.panic_type, fingerprint.top_frame, fingerprint.commit_hash]
        )
        query_embedding = embed_text(signature)

        best_score = -1.0
        best_match: dict[str, object] | None = None
        for _, resolution, embedding in self._resolved_errors:
            score = cosine_similarity(query_embedding, embedding)
            if score > best_score:
                best_score = score
                best_match = resolution

        if best_match is None or best_score < 0.95:
            return {"match": None, "score": best_score, "fingerprint": fingerprint.model_dump()}
        return {"match": best_match, "score": best_score, "fingerprint": fingerprint.model_dump()}


def _build_semantic_index():
    if settings.semantic_index_backend == "weaviate":
        from app.rag.retrieval.weaviate_semantic import WeaviateSemanticIndex
        return WeaviateSemanticIndex(url=settings.weaviate_url)
    return InMemorySemanticIndex()


semantic_index = _build_semantic_index()


def lookup_resolved_error(fingerprint: IncidentFingerprint) -> dict[str, object]:
    if hasattr(semantic_index, 'lookup_resolved_error'):
        return semantic_index.lookup_resolved_error(fingerprint)
    
    results = semantic_index.query_resolutions(
        f"{fingerprint.service} {fingerprint.panic_type} {fingerprint.top_frame}",
        limit=1
    )
    if not results or float(results[0].get("score", "0")) < 0.95:
        return {"match": None, "score": 0.0, "fingerprint": fingerprint.model_dump()}
    return {"match": results[0], "score": float(results[0]["score"]), "fingerprint": fingerprint.model_dump()}