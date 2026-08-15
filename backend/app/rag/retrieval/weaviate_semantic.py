import logging
import threading
import time
import uuid

import numpy as np
from sentence_transformers import SentenceTransformer

import weaviate
from weaviate.classes.config import Configure, DataType, Property, Tokenization
from weaviate.classes.query import MetadataQuery

from app.schemas.codebase import CodeChunk, SemanticMatch
from app.schemas.incident import ResolutionPackage

logger = logging.getLogger(__name__)

_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "oncall-assistant.local")

_EMBED_MODEL = None
_EMBED_MODEL_NAME = "BAAI/bge-base-en-v1.5"
_EMBED_DIMENSIONS = 768


def _get_embed_model():
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        logger.info("Loading sentence-transformers model (%s)...", _EMBED_MODEL_NAME)
        _EMBED_MODEL = SentenceTransformer(_EMBED_MODEL_NAME)
        logger.info("Model loaded: %s (dim=%d)", _EMBED_MODEL_NAME, _EMBED_DIMENSIONS)
    return _EMBED_MODEL


def _enrich_text(symbol_id: str, content: str) -> str:
    return f"module: {symbol_id}\n{content}"


def embed_texts(texts: list[str]) -> list[list[float]]:
    model = _get_embed_model()
    vecs = model.encode(
        texts,
        convert_to_numpy=True,
        normalize_embeddings=True,
        show_progress_bar=False,
        batch_size=64,
    )
    return vecs.tolist()


def embed_text(text: str) -> list[float]:
    return embed_texts([text])[0]


def _code_chunk_schema():
    return [
        Property(name="symbol_id", data_type=DataType.TEXT, tokenization=Tokenization.FIELD),
        Property(name="file_path", data_type=DataType.TEXT, tokenization=Tokenization.FIELD),
        Property(name="language", data_type=DataType.TEXT, tokenization=Tokenization.FIELD),
        Property(name="kind", data_type=DataType.TEXT, tokenization=Tokenization.FIELD),
        Property(name="content", data_type=DataType.TEXT, tokenization=Tokenization.WORD),
        Property(name="start_line", data_type=DataType.INT),
        Property(name="end_line", data_type=DataType.INT),
    ]


class WeaviateSemanticIndex:
    def __init__(self, url: str = "http://localhost:8080") -> None:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or 8080
        self._client = None
        self._active_collection = "CodeChunk"
        self._shadow_collection: str | None = None
        self._lock = threading.Lock()
        self._collection_history: list[str] = []

    def _detect_active_collection(self) -> str:
        try:
            client = self._get_client()
            collections = client.collections.list_all()
            if "CodeChunk" not in collections and "CodeChunkB" in collections:
                logger.info("CodeChunk missing, CodeChunkB exists — using CodeChunkB as active")
                return "CodeChunkB"
            if "CodeChunk" in collections and "CodeChunkB" in collections:
                cc = client.collections.get("CodeChunk")
                ccb = client.collections.get("CodeChunkB")
                cc_count = cc.aggregate.over_all().total_count
                ccb_count = ccb.aggregate.over_all().total_count
                if ccb_count > cc_count:
                    logger.info("CodeChunkB has %d objects vs CodeChunk has %d — using CodeChunkB", ccb_count, cc_count)
                    return "CodeChunkB"
        except Exception:
            pass
        return "CodeChunk"

    def _get_client(self):
        if self._client is None:
            from weaviate.classes.init import AdditionalConfig, Timeout
            self._client = weaviate.connect_to_local(
                host=self._host,
                port=self._port,
                additional_config=AdditionalConfig(
                    timeout=Timeout(init=60, query=180, insert=600),
                ),
            )
            self._ensure_schema()
            self._active_collection = self._detect_active_collection()
        return self._client

    def _create_code_chunk_collection(self, name: str) -> None:
        client = self._get_client()
        collections = client.collections.list_all()
        if name not in collections:
            client.collections.create(
                name=name,
                properties=_code_chunk_schema(),
                vectorizer_config=Configure.Vectorizer.none(),
            )

    def _ensure_schema(self) -> None:
        if self._client is None:
            return
        self._create_code_chunk_collection(self._active_collection)

        collections = self._client.collections.list_all()
        if "ResolutionPackage" not in collections:
            self._client.collections.create(
                name="ResolutionPackage",
                properties=[
                    Property(name="fingerprint_key", data_type=DataType.TEXT),
                    Property(name="root_cause", data_type=DataType.TEXT),
                    Property(name="patch", data_type=DataType.TEXT),
                    Property(name="service", data_type=DataType.TEXT),
                    Property(name="panic_type", data_type=DataType.TEXT),
                ],
                vectorizer_config=Configure.Vectorizer.none(),
            )

    # ---- zero-downtime rebuild ----

    def begin_rebuild(self) -> None:
        """Create a shadow collection so reads keep hitting the old one."""
        with self._lock:
            shadow_name = "CodeChunkB" if self._active_collection == "CodeChunk" else "CodeChunk"
            try:
                self._get_client().collections.delete(shadow_name)
            except Exception:
                pass
            self._create_code_chunk_collection(shadow_name)
            self._shadow_collection = shadow_name
            logger.info(
                "Shadow collection '%s' created for zero-downtime rebuild (active: '%s')",
                shadow_name, self._active_collection,
            )

    def commit_rebuild(self) -> None:
        """Atomically swap shadow -> active. Old collection is kept for rollback."""
        with self._lock:
            if not self._shadow_collection:
                logger.warning("commit_rebuild called but no shadow collection is set")
                return
            old = self._active_collection
            self._collection_history.append(old)
            self._active_collection = self._shadow_collection
            self._shadow_collection = None
            if hasattr(self, '_collection_cache'):
                self._collection_cache.pop(old, None)
            logger.info(
                "Swapped active collection '%s' -> '%s' (old kept for rollback, history depth=%d)",
                old, self._active_collection, len(self._collection_history),
            )

    def rollback_collection(self) -> str | None:
        """Rollback to the previous active collection. Returns the restored name or None."""
        with self._lock:
            if not self._collection_history:
                logger.warning("Rollback attempted but no collection history available")
                return None
            current = self._active_collection
            previous = self._collection_history.pop()
            self._active_collection = previous
            logger.warning("Rolled back collection '%s' -> '%s'", current, previous)
            return previous

    def get_active_collection_name(self) -> str:
        return self._active_collection

    # ---- branch-aware indexing ----

    def upsert_branch_chunks(self, chunks: list[CodeChunk], branch_name: str) -> None:
        """Index branch-specific chunks into a separate collection."""
        col_name = f"CodeChunk_{branch_name}"[:63]
        self._create_code_chunk_collection(col_name)
        collection = self._get_client().collections.get(col_name)
        batch_size = 20
        embed_batch_size = 64

        for i in range(0, len(chunks), embed_batch_size):
            batch_chunks = chunks[i:i + embed_batch_size]
            enriched = [_enrich_text(c.symbol_id, c.content) for c in batch_chunks]
            embeddings = embed_texts(enriched)
            with collection.batch.fixed_size(batch_size=batch_size) as batch:
                for chunk, vec in zip(batch_chunks, embeddings):
                    obj_uuid = str(uuid.uuid5(_UUID_NAMESPACE, f"{branch_name}:{chunk.symbol_id}"))
                    batch.add_object(
                        properties={
                            "symbol_id": chunk.symbol_id,
                            "file_path": chunk.file_path,
                            "language": chunk.language,
                            "kind": chunk.kind,
                            "content": chunk.content,
                            "start_line": chunk.start_line,
                            "end_line": chunk.end_line,
                        },
                        uuid=obj_uuid,
                        vector=vec,
                    )
            logger.info("Branch index: %d/%d chunks (collection: %s)", min(i + embed_batch_size, len(chunks)), len(chunks), col_name)

    def query_branch_chunks(self, query: str, branch_name: str, limit: int = 5) -> list[SemanticMatch]:
        """Query branch-specific collection."""
        col_name = f"CodeChunk_{branch_name}"[:63]
        try:
            collection = self._get_client().collections.get(col_name)
        except Exception:
            return []
        query_vec = embed_text(query)
        response = collection.query.hybrid(
            query=query,
            vector=query_vec,
            alpha=0.5,
            limit=limit,
            return_metadata=MetadataQuery(distance=True, score=True),
        )
        results: list[SemanticMatch] = []
        for obj in response.objects:
            dist = obj.metadata.distance or 0.0
            score = max(0.0, 1.0 - dist)
            results.append(
                SemanticMatch(
                    symbol_id=obj.properties["symbol_id"],
                    score=score,
                    content=obj.properties["content"],
                    source=obj.properties["file_path"],
                )
            )
        return results

    def query_all_collections(self, query: str, limit: int = 5) -> list[SemanticMatch]:
        """Query active collection + all branch collections, merge results."""
        results = self.query_chunks(query, limit=limit)

        try:
            all_cols = self._get_client().collections.list_all()
            branch_cols = [c for c in all_cols if c.startswith("CodeChunk_")]
            for col_name in branch_cols:
                try:
                    collection = self._get_client().collections.get(col_name)
                    query_vec = embed_text(query)
                    response = collection.query.hybrid(
                        query=query,
                        vector=query_vec,
                        alpha=0.5,
                        limit=limit,
                        return_metadata=MetadataQuery(distance=True, score=True),
                    )
                    for obj in response.objects:
                        dist = obj.metadata.distance or 0.0
                        score = max(0.0, 1.0 - dist)
                        results.append(
                            SemanticMatch(
                                symbol_id=obj.properties["symbol_id"],
                                score=score,
                                content=obj.properties["content"],
                                source=obj.properties["file_path"],
                            )
                        )
                except Exception:
                    pass
        except Exception:
            pass

        results.sort(key=lambda m: m.score, reverse=True)
        return results[:limit]

    def clear_branch_index(self, branch_name: str) -> None:
        col_name = f"CodeChunk_{branch_name}"[:63]
        try:
            self._get_client().collections.delete(col_name)
            logger.info("Cleared branch collection: %s", col_name)
        except Exception:
            pass

    def reset_documents(self) -> None:
        """Backward-compatible entry point — now uses shadow pattern."""
        self.begin_rebuild()

    # ---- writes ----

    def _get_collection(self, name: str):
        if not hasattr(self, '_collection_cache'):
            self._collection_cache = {}
        if name not in self._collection_cache:
            self._collection_cache[name] = self._get_client().collections.get(name)
        return self._collection_cache[name]

    def upsert_chunks(self, chunks: list[CodeChunk]) -> None:
        write_name = self._shadow_collection or self._active_collection
        collection = self._get_collection(write_name)
        batch_size = 20
        embed_batch_size = 64

        for i in range(0, len(chunks), embed_batch_size):
            batch_chunks = chunks[i:i + embed_batch_size]
            enriched = [_enrich_text(c.symbol_id, c.content) for c in batch_chunks]
            embeddings = embed_texts(enriched)

            with collection.batch.fixed_size(batch_size=batch_size) as batch:
                for chunk, vec in zip(batch_chunks, embeddings):
                    obj_uuid = str(uuid.uuid5(_UUID_NAMESPACE, chunk.symbol_id))
                    batch.add_object(
                        properties={
                            "symbol_id": chunk.symbol_id,
                            "file_path": chunk.file_path,
                            "language": chunk.language,
                            "kind": chunk.kind,
                            "content": chunk.content,
                            "start_line": chunk.start_line,
                            "end_line": chunk.end_line,
                        },
                        uuid=obj_uuid,
                        vector=vec,
                    )
            failed = batch.number_errors
            if failed:
                logger.warning("Batch insert: %d objects failed out of %d chunks (batch %d)", failed, len(batch_chunks), i // embed_batch_size)
            logger.info("Embedded and inserted %d/%d chunks (collection: %s)", min(i + embed_batch_size, len(chunks)), len(chunks), write_name)

    # ---- reads (always hit active collection) ----

    def query_chunks(self, query: str, limit: int = 5) -> list[SemanticMatch]:
        collection = self._get_client().collections.get(self._active_collection)
        query_vec = embed_text(query)

        response = collection.query.hybrid(
            query=query,
            vector=query_vec,
            alpha=0.5,
            limit=limit,
            return_metadata=MetadataQuery(distance=True, score=True),
        )

        results: list[SemanticMatch] = []
        for obj in response.objects:
            dist = obj.metadata.distance or 0.0
            score = max(0.0, 1.0 - dist)
            results.append(
                SemanticMatch(
                    symbol_id=obj.properties["symbol_id"],
                    score=score,
                    content=obj.properties["content"],
                    source=obj.properties["file_path"],
                )
            )
        return results

    def add_resolution_package(self, package: ResolutionPackage) -> None:
        collection = self._get_client().collections.get("ResolutionPackage")
        collection.data.insert(
            properties={
                "fingerprint_key": package.fingerprint_key,
                "root_cause": package.root_cause,
                "patch": package.patch,
                "service": package.fingerprint.service,
                "panic_type": package.fingerprint.panic_type,
            },
            uuid=package.fingerprint_key.replace(":", "_"),
        )

    def query_resolutions(self, query: str, limit: int = 3) -> list[dict[str, str]]:
        collection = self._get_client().collections.get("ResolutionPackage")
        response = collection.query.near_vector(
            near_vector=embed_text(query),
            limit=limit,
            return_metadata=MetadataQuery(distance=True),
        )

        results: list[dict[str, str]] = []
        for obj in response.objects:
            dist = obj.metadata.distance or 0.0
            results.append(
                {
                    "fingerprint_key": obj.properties["fingerprint_key"],
                    "root_cause": obj.properties["root_cause"],
                    "patch": obj.properties["patch"],
                    "match": f"{obj.properties['service']}::{obj.properties['panic_type']}",
                    "score": str(max(0.0, 1.0 - dist)),
                }
            )
        return results

    def get_stats(self) -> dict[str, int]:
        try:
            chunks = self._get_client().collections.get(self._active_collection)
            resolutions = self._get_client().collections.get("ResolutionPackage")
            chunk_agg = chunks.aggregate.over_all()
            resolution_agg = resolutions.aggregate.over_all()
            return {
                "semantic_documents": chunk_agg.total_count if hasattr(chunk_agg, 'total_count') else 0,
                "resolved_error_documents": resolution_agg.total_count if hasattr(resolution_agg, 'total_count') else 0,
            }
        except Exception:
            return {"semantic_documents": 0, "resolved_error_documents": 0}

    def has_symbol(self, symbol_id: str) -> bool:
        collection = self._get_client().collections.get(self._active_collection)
        try:
            response = collection.query.fetch_objects(
                filters={"path": ["symbol_id"], "operator": "Equal", "valueText": symbol_id},
                limit=1,
            )
            return len(response.objects) > 0
        except Exception:
            return False

    def get_chunk(self, symbol_id: str) -> CodeChunk | None:
        collection = self._get_client().collections.get(self._active_collection)
        try:
            response = collection.query.fetch_objects(
                filters={"path": ["symbol_id"], "operator": "Equal", "valueText": symbol_id},
                limit=1,
            )
            if not response.objects:
                return None
            obj = response.objects[0]
            return CodeChunk(
                symbol_id=obj.properties["symbol_id"],
                file_path=obj.properties["file_path"],
                language=obj.properties["language"],
                kind=obj.properties["kind"],
                content=obj.properties["content"],
                start_line=obj.properties["start_line"],
                end_line=obj.properties["end_line"],
            )
        except Exception:
            return None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
