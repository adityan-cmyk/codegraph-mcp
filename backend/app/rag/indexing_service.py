from pathlib import Path
import logging
import os
import re
import threading
import time

from app.core.index_store import index_metadata_store
from app.core.config import ROOT_DIR, settings
from app.rag.ingestion.tree_sitter import extract_rust_chunks, generate_file_summary_chunk, generate_module_exports_chunk, DERIVE_TRAIT_NAMES, _extract_use_statements
from app.rag.retrieval.graph import graph_index
from app.rag.retrieval.semantic import semantic_index
from app.schemas.codebase import CodeChunk, GraphEdge, IndexSnapshot, IndexingResult

logger = logging.getLogger(__name__)

_semantic_rebuild_lock = threading.Lock()
_semantic_rebuild_in_progress = False


CALL_PATTERN = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?:!\s*)?\(")
METHOD_CALL_PATTERN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")
TYPE_REF_PATTERN = re.compile(
    r"(?:"
    r"\b(?:impl|dyn|as|where|:\s*|->\s*|<|,\s*)"
    r"([A-Z][A-Za-z0-9_]*)"
    r")"
    r"|(?:"
    r"\b([A-Z][A-Za-z0-9_]*)\s*::\s*[A-Z][A-Za-z0-9_]*"
    r")"
)
RUST_EXTENSIONS = {".rs"}
RUST_KEYWORDS = {
    "if",
    "match",
    "loop",
    "while",
    "for",
    "return",
    "println",
    "format",
    "panic",
    "assert",
}
RUST_TYPE_KEYWORDS = {
    "Self",
    "Option",
    "Result",
    "Vec",
    "Box",
    "Rc",
    "Arc",
    "String",
    "Error",
    "Ok",
    "Err",
    "Some",
    "None",
    "True",
    "False",
    "BufReader",
    "BufWriter",
    "HashMap",
    "HashSet",
    "BTreeMap",
    "BTreeSet",
}

RUST_GENERIC_METHODS = {
    "new",
    "from",
    "into",
    "clone",
    "default",
    "drop",
    "as_ref",
    "as_mut",
    "from_str",
    "to_string",
    "into_inner",
    "is_empty",
    "len",
    "get",
    "set",
    "push",
    "pop",
    "insert",
    "remove",
    "contains",
    "iter",
    "next",
    "collect",
    "map",
    "filter",
    "unwrap",
    "unwrap_or",
    "unwrap_or_else",
    "unwrap_or_default",
    "expect",
    "ok",
    "err",
    "is_ok",
    "is_err",
    "is_some",
    "is_none",
    "as_str",
    "from_env",
    "success",
    "code",
    "limit",
    "spawn",
    "join",
    "lock",
    "unlock",
    "read",
    "write",
    "close",
    "open",
    "send",
    "recv",
    "connect",
    "bind",
    "listen",
    "accept",
    "handle",
    "init",
    "start",
    "stop",
    "run",
    "execute",
    "build",
    "parse",
    "serialize",
    "deserialize",
    "Err",
    "Ok",
    "Some",
    "None",
    "error",
    "validate",
    "fmt",
    "encode",
    "decode",
}


class IndexingSecurityError(ValueError):
    pass


def _allowed_root_paths() -> list[Path]:
    raw_roots = list(settings.indexing_allowed_roots)
    if settings.codebase_root_path:
        raw_roots.append(settings.codebase_root_path)
    if not raw_roots:
        raw_roots = [str(ROOT_DIR)]
    return [Path(root).expanduser().resolve() for root in raw_roots if Path(root).expanduser().resolve() != Path("/")]


def resolve_repository_path(repository_path: str | None) -> Path:
    target = Path(repository_path or settings.codebase_root_path or ROOT_DIR).expanduser().resolve()
    allowed_roots = _allowed_root_paths()
    if not any(root == target or root in target.parents for root in allowed_roots):
        raise IndexingSecurityError("Repository path is outside configured indexing roots.")
    return target


def _discover_rust_files(repository_path: Path) -> list[Path]:
    skip_dirs = {".git", "target", "node_modules", "__pycache__", "tests", "test"}
    skip_suffixes = ("_test.rs", "_tests.rs", "tests.rs", "test.rs")
    return sorted(
        path for path in repository_path.rglob("*")
        if path.suffix in RUST_EXTENSIONS
        and path.is_file()
        and not any(part in skip_dirs for part in path.relative_to(repository_path).parts)
        and not path.name.endswith(skip_suffixes)
    )


def _build_directory_tree(repository_path: Path, max_depth: int = 3) -> str:
    lines: list[str] = []
    skip_dirs = {".git", "target", "node_modules", "__pycache__"}
    for dirpath, dirnames, filenames in os.walk(repository_path):
        dirnames[:] = [d for d in sorted(dirnames) if d not in skip_dirs]
        rel = os.path.relpath(dirpath, repository_path)
        depth = rel.count(os.sep) + (0 if rel == "." else 1)
        if depth > max_depth:
            dirnames.clear()
            continue
        indent = "  " * depth
        dirname = os.path.basename(dirpath) if rel != "." else str(repository_path.name)
        rs_files = sorted(f for f in filenames if f.endswith(".rs"))
        toml_files = sorted(f for f in filenames if f == "Cargo.toml")
        if toml_files or rs_files:
            lines.append(f"{indent}{dirname}/")
            for f in toml_files:
                lines.append(f"{indent}  {f}")
            if len(rs_files) <= 8:
                for f in rs_files:
                    lines.append(f"{indent}  {f}")
            else:
                for f in rs_files[:6]:
                    lines.append(f"{indent}  {f}")
                lines.append(f"{indent}  ... ({len(rs_files) - 6} more .rs files)")
    return "\n".join(lines[:60])


def _symbol_name(symbol_id: str) -> str:
    return symbol_id.split("::")[-1]


def _build_import_map(use_statements: list[str]) -> dict[str, str]:
    """Build a mapping from short imported name to full path."""
    import_map: dict[str, str] = {}
    for stmt in use_statements:
        stmt = stmt.strip()
        if "::" not in stmt:
            continue
        if " as " in stmt:
            parts = stmt.split(" as ")
            full_path = parts[0].strip()
            alias = parts[1].strip()
            import_map[alias] = full_path
        else:
            short_name = stmt.split("::")[-1].strip()
            if short_name == "*":
                continue
            import_map[short_name] = stmt
        if "::{" in stmt:
            inner = stmt.split("::{")[-1].rstrip("}")
            prefix = stmt.split("::{")[0]
            for item in inner.split(","):
                item = item.strip()
                if " as " in item:
                    parts = item.split(" as ")
                    import_map[parts[1].strip()] = f"{prefix}::{parts[0].strip()}"
                else:
                    short = item.split("::")[-1].strip()
                    import_map[short] = f"{prefix}::{item}"
    return import_map


GRAPH_SKIP_KINDS = frozenset({"file_summary", "module_exports"})


def _should_skip_for_graph(chunk: CodeChunk) -> bool:
    if chunk.kind in GRAPH_SKIP_KINDS:
        return True
    short_name = _symbol_name(chunk.symbol_id)
    if short_name in ("Relation",):
        return True
    if short_name == "new" and chunk.kind == "fn" and len(chunk.content.strip()) < 50:
        return True
    return False


def _extract_call_targets(chunk: CodeChunk, name_index: dict[str, list[str]]) -> list[str]:
    calls: list[str] = []
    for match in CALL_PATTERN.finditer(chunk.content):
        candidate = match.group(1)
        if candidate in RUST_KEYWORDS or candidate in RUST_GENERIC_METHODS:
            continue
        if candidate in DERIVE_TRAIT_NAMES:
            continue
        for symbol_id in name_index.get(candidate, []):
            if symbol_id != chunk.symbol_id and symbol_id not in calls:
                calls.append(symbol_id)
    for match in METHOD_CALL_PATTERN.finditer(chunk.content):
        candidate = match.group(1)
        if candidate in RUST_KEYWORDS or candidate in RUST_GENERIC_METHODS:
            continue
        for symbol_id in name_index.get(candidate, []):
            if symbol_id != chunk.symbol_id and symbol_id not in calls:
                calls.append(symbol_id)
    return calls


def _extract_type_references(chunk: CodeChunk, name_index: dict[str, list[str]]) -> list[str]:
    uses: list[str] = []
    for match in TYPE_REF_PATTERN.finditer(chunk.content):
        candidate = match.group(1) or match.group(2)
        if not candidate:
            continue
        if candidate in RUST_TYPE_KEYWORDS or candidate in RUST_KEYWORDS:
            continue
        if candidate in DERIVE_TRAIT_NAMES:
            continue
        for symbol_id in name_index.get(candidate, []):
            if symbol_id != chunk.symbol_id and symbol_id not in uses and symbol_id not in chunk.symbol_id.split("::")[:-1]:
                uses.append(symbol_id)
    return uses


def _classify_usage_modes(content: str, target_short_name: str) -> list[str]:
    """Classify how a symbol is referenced in the source content.

    Returns a list of usage modes: pattern_match, construction, field_declaration,
    trait_impl, type_param, import, type_reference.
    """
    if not target_short_name or target_short_name in RUST_TYPE_KEYWORDS:
        return ["reference"]

    modes: list[str] = []
    name = re.escape(target_short_name)

    if re.search(rf"\bmatch\s+\w+[^;]*\b{name}\b::", content, re.DOTALL):
        modes.append("pattern_match")
    if re.search(rf"\b{name}\s*::\s*\w+\s*\(", content):
        modes.append("construction")
    if re.search(rf"^\s*(?:pub\s+)?\w+\s*:\s*(?:Option<)?\s*\b{name}\b", content, re.MULTILINE):
        modes.append("field_declaration")
    if re.search(rf"\bimpl\b[^\n]*\b{name}\b\s+for\s+", content):
        modes.append("trait_impl")
    if re.search(rf"<\s*[^>]*\b{name}\b", content):
        modes.append("type_param")
    if re.search(rf"^\s*(?:pub\s+)?use\s+.*\b{name}\b", content, re.MULTILINE):
        modes.append("import")

    if re.search(rf"\b{name}\b", content) and not modes:
        modes.append("type_reference")

    return modes if modes else ["reference"]


def _extract_type_references_with_modes(chunk: CodeChunk, name_index: dict[str, list[str]]) -> list[tuple[str, list[str]]]:
    """Extract type references with usage mode classification.

    Returns list of (symbol_id, usage_modes) tuples.
    """
    results: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for match in TYPE_REF_PATTERN.finditer(chunk.content):
        candidate = match.group(1) or match.group(2)
        if not candidate:
            continue
        if candidate in RUST_TYPE_KEYWORDS or candidate in RUST_KEYWORDS:
            continue
        if candidate in DERIVE_TRAIT_NAMES:
            continue
        for symbol_id in name_index.get(candidate, []):
            if symbol_id != chunk.symbol_id and symbol_id not in seen and symbol_id not in chunk.symbol_id.split("::")[:-1]:
                seen.add(symbol_id)
                modes = _classify_usage_modes(chunk.content, candidate)
                results.append((symbol_id, modes))
    return results


def _rebuild_graph_and_semantic_parallel(snapshot: IndexSnapshot, *, force: bool = False) -> dict[str, int]:
    _start_semantic_rebuild_background(snapshot, force=force)

    from app.rag.retrieval.graph import _build_graph_index, _get_current_gen, graph_index

    build_id = None
    try:
        from app.rag.reinforcement import build_registry
        active_build = build_registry.get_active_build()
        parent_build_id = active_build["build_id"] if active_build else None
    except Exception:
        parent_build_id = None

    current_gen = _get_current_gen()
    new_gen = current_gen + 1
    logger.info("Building new graph (gen %d) alongside existing (gen %d) — zero-downtime", new_gen, current_gen)

    if hasattr(semantic_index, 'get_active_collection_name'):
        weaviate_col = semantic_index.get_active_collection_name()
    else:
        weaviate_col = None

    try:
        build_id = build_registry.register_build(
            build_type="both",
            parent_build_id=parent_build_id,
            weaviate_collection=weaviate_col,
            neo4j_gen=new_gen,
        )
    except Exception:
        build_id = None

    new_graph = _build_graph_index(gen=new_gen)
    new_graph.reset()

    calls_by_source: dict[str, list[str]] = {}
    uses_with_modes_by_source: dict[str, list[tuple[str, list[str]]]] = {}
    for edge in snapshot.graph_edges:
        if edge.relation == "uses":
            uses_with_modes_by_source.setdefault(edge.source_symbol_id, []).append(
                (edge.target_symbol_id, edge.usage_modes or ["reference"])
            )
        else:
            calls_by_source.setdefault(edge.source_symbol_id, []).append(edge.target_symbol_id)
    chunk_meta: dict[str, dict] = {}
    for chunk in snapshot.chunks:
        chunk_meta[chunk.symbol_id] = {
            "kind": chunk.kind,
            "file_path": chunk.file_path,
            "start_line": chunk.start_line,
            "end_line": chunk.end_line,
            "module": chunk.file_path.removesuffix(".rs").replace("/", "::"),
        }

    for chunk in snapshot.chunks:
        if _should_skip_for_graph(chunk):
            continue
        new_graph.upsert_symbol(
            chunk.symbol_id,
            calls=calls_by_source.get(chunk.symbol_id, []),
            uses_with_modes=uses_with_modes_by_source.get(chunk.symbol_id, []),
            metadata=chunk_meta.get(chunk.symbol_id),
        )

    new_stats = new_graph.get_stats()
    logger.info("New graph (gen %d) built: %d nodes, %d edges — swapping", new_gen, new_stats["graph_nodes"], new_stats["graph_edges"])

    old_graph = graph_index.swap(new_graph, build_id=build_id)

    def _cleanup_old_gen():
        try:
            old_graph.cleanup_other_gens()
            logger.info("Cleaned up graph generations older than current (gen %d)", new_gen)
        except Exception:
            logger.warning("Failed to clean up old graph generation", exc_info=True)
        if hasattr(old_graph, "close"):
            old_graph.close()

    threading.Thread(target=_cleanup_old_gen, daemon=True, name=f"graph-cleanup-gen{current_gen}").start()

    if build_id:
        try:
            build_registry.complete_build(
                build_id,
                chunk_count=len(snapshot.chunks),
                graph_nodes=new_stats["graph_nodes"],
                graph_edges=new_stats["graph_edges"],
            )
        except Exception:
            logger.warning("Failed to register build completion", exc_info=True)

    return new_stats


def _wait_for_weaviate_ready() -> None:
    import httpx
    for wait in range(30):
        try:
            resp = httpx.get(f"{settings.weaviate_url}/v1/.well-known/ready", timeout=5)
            if resp.status_code == 200:
                logger.info("Weaviate is ready")
                return
        except Exception:
            pass
        logger.info("Waiting for weaviate to be ready (attempt %d/30)", wait + 1)
        time.sleep(10)
    logger.warning("Weaviate did not become ready after 5 minutes, proceeding anyway")


def _rebuild_semantic_index(snapshot: IndexSnapshot, *, force: bool = False) -> None:
    global _semantic_rebuild_in_progress
    try:
        _wait_for_weaviate_ready()
        if not force:
            existing_stats = semantic_index.get_stats()
            existing_docs = existing_stats.get("semantic_documents", 0)
            if existing_docs > 0:
                logger.info("Weaviate already has %d docs and snapshot exists — skipping rebuild", existing_docs)
                return
        logger.info("Rebuilding semantic index from snapshot (%d chunks, force=%s)", len(snapshot.chunks), force)

        if hasattr(semantic_index, 'begin_rebuild'):
            semantic_index.begin_rebuild()
            logger.info("Shadow collection created — old data still serving queries")
            semantic_index.upsert_chunks(snapshot.chunks)
            semantic_index.commit_rebuild()
            logger.info("Semantic rebuild committed — new collection is now active")
        else:
            if force:
                semantic_index.reset_documents()
            semantic_index.upsert_chunks(snapshot.chunks)
    except Exception:
        logger.exception("Semantic index rebuild failed")
    finally:
        with _semantic_rebuild_lock:
            _semantic_rebuild_in_progress = False
            logger.info("Semantic rebuild complete")


def _start_semantic_rebuild_background(snapshot: IndexSnapshot, *, force: bool = False) -> None:
    global _semantic_rebuild_in_progress
    with _semantic_rebuild_lock:
        if _semantic_rebuild_in_progress:
            logger.warning("Semantic rebuild already in progress, skipping")
            return
        _semantic_rebuild_in_progress = True
    logger.info("Starting semantic rebuild in background (%d chunks, force=%s)", len(snapshot.chunks), force)
    thread = threading.Thread(target=_rebuild_semantic_index, args=(snapshot,), kwargs={"force": force}, daemon=True)
    thread.start()


def semantic_rebuild_is_in_progress() -> bool:
    with _semantic_rebuild_lock:
        return _semantic_rebuild_in_progress


def index_rust_repository(repository_path: str | None = None) -> IndexingResult:
    target_path = resolve_repository_path(repository_path)
    rust_files = _discover_rust_files(target_path)

    all_chunks: list[CodeChunk] = []
    name_index: dict[str, list[str]] = {}

    for file_path in rust_files:
        relative_path = file_path.relative_to(target_path).as_posix()
        source = file_path.read_text(encoding="utf-8")
        chunks = extract_rust_chunks(relative_path, source)
        file_summary = generate_file_summary_chunk(relative_path, source, chunks)
        if file_summary:
            chunks.append(file_summary)
        module_exports = generate_module_exports_chunk(relative_path, source)
        if module_exports:
            chunks.append(module_exports)
        all_chunks.extend(chunks)
        for chunk in chunks:
            name_index.setdefault(_symbol_name(chunk.symbol_id), []).append(chunk.symbol_id)

    graph_edges: list[GraphEdge] = []
    for chunk in all_chunks:
        if _should_skip_for_graph(chunk):
            continue
        calls = _extract_call_targets(chunk, name_index)
        graph_edges.extend(
            GraphEdge(source_symbol_id=chunk.symbol_id, target_symbol_id=target_symbol_id, relation="calls")
            for target_symbol_id in calls
        )
        uses_with_modes = _extract_type_references_with_modes(chunk, name_index)
        graph_edges.extend(
            GraphEdge(source_symbol_id=chunk.symbol_id, target_symbol_id=sid, relation="uses", usage_modes=modes)
            for sid, modes in uses_with_modes
        )

    head_commit = ""
    try:
        head_result = __import__("subprocess").run(
            ["git", "rev-parse", "HEAD"],
            cwd=target_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if head_result.returncode == 0:
            head_commit = head_result.stdout.strip()
    except Exception:
        pass

    snapshot = IndexSnapshot(
        repository_path=str(target_path),
        files_indexed=len(rust_files),
        chunks=all_chunks,
        graph_edges=graph_edges,
        last_indexed_commit=head_commit,
    )
    index_metadata_store.replace_snapshot(snapshot)

    graph_stats = _rebuild_graph_and_semantic_parallel(snapshot, force=True)

    return IndexingResult(
        symbols_indexed=len(snapshot.chunks),
        semantic_documents=0,
        graph_nodes=graph_stats["graph_nodes"],
        graph_edges=graph_stats["graph_edges"],
        files_indexed=snapshot.files_indexed,
        repository_path=snapshot.repository_path,
    )


def replay_indexes_from_storage() -> IndexingResult | None:
    snapshot = index_metadata_store.load_snapshot()
    if snapshot is None:
        semantic_index.reset_documents()
        graph_index.reset()
        return None
    graph_stats = _rebuild_graph_and_semantic_parallel(snapshot)
    return IndexingResult(
        symbols_indexed=len(snapshot.chunks),
        semantic_documents=0,
        graph_nodes=graph_stats["graph_nodes"],
        graph_edges=graph_stats["graph_edges"],
        files_indexed=snapshot.files_indexed,
        repository_path=snapshot.repository_path,
    )


def reindex_semantic_only() -> IndexingResult | None:
    snapshot = index_metadata_store.load_snapshot()
    if snapshot is None:
        return None
    _start_semantic_rebuild_background(snapshot, force=True)
    graph_stats = graph_index.get_stats()
    return IndexingResult(
        symbols_indexed=len(snapshot.chunks),
        semantic_documents=0,
        graph_nodes=graph_stats["graph_nodes"],
        graph_edges=graph_stats["graph_edges"],
        files_indexed=snapshot.files_indexed,
        repository_path=snapshot.repository_path,
    )


def rollback_last_build(reason: str = "quality regression detected") -> dict[str, object]:
    """Rollback to the previous build — restores old Weaviate collection and Neo4j gen.

    This is the idempotency mechanism: if a new build produces worse results
    (based on feedback quality scores), we revert to the previous build's
    data without re-indexing.
    """
    from app.rag.reinforcement import build_registry

    active = build_registry.get_active_build()
    if not active:
        return {"error": "No active build to rollback from"}

    build_id = active["build_id"]
    parent = build_registry.rollback_build(build_id, reason)
    if not parent:
        return {"error": "No parent build available for rollback"}

    if hasattr(semantic_index, 'rollback_collection'):
        restored_col = semantic_index.rollback_collection()
    else:
        restored_col = None

    result = graph_index.rollback()
    if result:
        old_backend, current_backend = result
        if hasattr(current_backend, 'close'):
            current_backend.close()

    logger.warning(
        "Rollback complete: build %s -> parent %s (weaviate=%s)",
        build_id, parent.get("build_id"), restored_col,
    )
    return {
        "rolled_back_build": build_id,
        "restored_build": parent.get("build_id"),
        "restored_weaviate_collection": restored_col,
        "reason": reason,
    }


def incremental_update_symbols(
    modified_files: list[str],
    repository_path: str | None = None,
) -> IndexingResult:
    """Update only the symbols in modified files without full reindex."""
    target_path = resolve_repository_path(repository_path)
    snapshot = index_metadata_store.load_snapshot()
    if snapshot is None:
        return index_rust_repository(repository_path)

    existing_chunks = list(snapshot.chunks)
    existing_edges = list(snapshot.graph_edges)
    
    modified_symbol_ids: set[str] = set()
    new_chunks: list[CodeChunk] = []
    name_index: dict[str, list[str]] = {}

    for chunk in existing_chunks:
        name_index.setdefault(_symbol_name(chunk.symbol_id), []).append(chunk.symbol_id)

    for file_path_str in modified_files:
        absolute_path = target_path / file_path_str
        if not absolute_path.exists():
            existing_chunks = [chunk for chunk in existing_chunks if chunk.file_path != file_path_str]
            continue

        source = absolute_path.read_text(encoding="utf-8")
        file_chunks = extract_rust_chunks(file_path_str, source)
        file_summary = generate_file_summary_chunk(file_path_str, source, file_chunks)
        if file_summary:
            file_chunks.append(file_summary)
        module_exports = generate_module_exports_chunk(file_path_str, source)
        if module_exports:
            file_chunks.append(module_exports)
        for chunk in file_chunks:
            modified_symbol_ids.add(chunk.symbol_id)
            new_chunks.append(chunk)
            name_index.setdefault(_symbol_name(chunk.symbol_id), []).append(chunk.symbol_id)

        existing_chunks = [chunk for chunk in existing_chunks if chunk.file_path != file_path_str]

    all_chunks = existing_chunks + new_chunks
    existing_edges = [edge for edge in existing_edges if edge.source_symbol_id not in modified_symbol_ids]

    new_edges: list[GraphEdge] = []
    for chunk in new_chunks:
        calls = _extract_call_targets(chunk, name_index)
        new_edges.extend(
            GraphEdge(source_symbol_id=chunk.symbol_id, target_symbol_id=target_id, relation="calls")
            for target_id in calls
        )
        uses_with_modes = _extract_type_references_with_modes(chunk, name_index)
        new_edges.extend(
            GraphEdge(source_symbol_id=chunk.symbol_id, target_symbol_id=sid, relation="uses", usage_modes=modes)
            for sid, modes in uses_with_modes
        )

    removed_edge_keys = [
        (e.source_symbol_id, e.target_symbol_id)
        for e in snapshot.graph_edges
        if e.source_symbol_id in modified_symbol_ids
    ]

    index_metadata_store.update_incremental(
        modified_chunks=new_chunks,
        removed_symbol_ids=list(modified_symbol_ids),
        modified_edges=new_edges,
        removed_edge_keys=removed_edge_keys,
    )

    for i in range(0, len(new_chunks), 64):
        semantic_index.upsert_chunks(new_chunks[i:i + 64])
        
    calls_by_source: dict[str, list[str]] = {}
    uses_with_modes_by_source: dict[str, list[tuple[str, list[str]]]] = {}
    for edge in new_edges:
        if edge.relation == "uses":
            uses_with_modes_by_source.setdefault(edge.source_symbol_id, []).append(
                (edge.target_symbol_id, edge.usage_modes or ["reference"])
            )
        else:
            calls_by_source.setdefault(edge.source_symbol_id, []).append(edge.target_symbol_id)

    for chunk in new_chunks:
        graph_index.upsert_symbol(
            chunk.symbol_id,
            calls=calls_by_source.get(chunk.symbol_id, []),
            uses_with_modes=uses_with_modes_by_source.get(chunk.symbol_id, []),
            metadata={
                "kind": chunk.kind,
                "file_path": chunk.file_path,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "module": chunk.file_path.removesuffix(".rs").replace("/", "::"),
            },
        )

    graph_stats = graph_index.get_stats()
    return IndexingResult(
        symbols_indexed=len(new_chunks),
        semantic_documents=len(new_chunks),
        graph_nodes=graph_stats["graph_nodes"],
        graph_edges=graph_stats["graph_edges"],
        files_indexed=len(modified_files),
        repository_path=str(target_path),
    )