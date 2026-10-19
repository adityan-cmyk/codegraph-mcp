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
from app.schemas.codebase import (
    GRAPH_SKIP_KINDS as _SCHEMA_GRAPH_SKIP_KINDS,
    CodeChunk,
    GraphEdge,
    IndexSnapshot,
    IndexingResult,
    compute_symbol_set_hash,
)

logger = logging.getLogger(__name__)

_semantic_rebuild_lock = threading.Lock()
_semantic_rebuild_in_progress = False


CALL_PATTERN = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?:!\s*)?\(")
METHOD_CALL_PATTERN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")
QUALIFIED_CALL_PATTERN = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)+)\s*(?:!\s*)?\(")

# Short names that must never resolve as call/type targets: pseudo-symbols,
# parser artifacts, and noise kinds.
_RESOLVE_SKIP_NAMES = frozenset(
    {"file_summary", "module_exports", "fn", "let", "match", "if", "Relation"}
)
# A short name mapping to more than this many symbols is a hub (new, from,
# generate...) — resolving it creates phantom edges to every same-named symbol.
HUB_LIMIT = 3
# Only these kinds may be CALL targets; type kinds resolve through USES.
_CALLABLE_KINDS = ("fn", "method", "impl")
_TYPE_KINDS = ("struct", "enum", "trait", "type", "impl", "fn", "method", "const", "static")


def _build_name_index(chunks: list[CodeChunk]) -> dict[str, list[tuple[str, str, str]]]:
    """short name -> [(symbol_id, file_path, kind)] — file info powers scoped
    resolution; kind powers call-vs-type disambiguation (constructing an enum
    variant or struct tuple is a USES edge, not a CALLS edge)."""
    idx: dict[str, list[tuple[str, str, str]]] = {}
    for c in chunks:
        idx.setdefault(_symbol_name(c.symbol_id), []).append((c.symbol_id, c.file_path, c.kind))
    return idx


def _build_path_index(chunks: list[CodeChunk]) -> dict[str, list[str]]:
    """'Impl::method' / 'module::fn' -> [symbol_ids] — precise qualified-call resolution."""
    idx: dict[str, list[str]] = {}
    for c in chunks:
        parts = c.symbol_id.split("::")
        if len(parts) >= 2:
            idx.setdefault(f"{parts[-2]}::{parts[-1]}", []).append(c.symbol_id)
    return idx


def _parent_dir(file_path: str) -> str:
    return file_path.rsplit("/", 1)[0] if "/" in file_path else ""


def _resolve_candidates(candidate: str, file_path: str, name_index: dict[str, list[tuple[str, str, str]]],
                        kinds: tuple[str, ...] | None = None) -> list[str]:
    """Scoped short-name resolution, in priority order:
    same file > same directory > small ambiguity (<= HUB_LIMIT) > drop.
    Hub names (new/from/generate with dozens of same-named symbols) used to
    resolve to EVERY candidate — the phantom-edge problem. `kinds` restricts
    resolution to those symbol kinds (calls resolve only fns/methods; enum
    variants and struct tuple constructors are type constructions, not calls)."""
    if candidate in _RESOLVE_SKIP_NAMES:
        return []
    entries = name_index.get(candidate) or []
    if not entries:
        return []
    if kinds is not None:
        entries = [e for e in entries if e[2] in kinds]
        if not entries:
            return []
    same_file = [sid for sid, f, _ in entries if f == file_path]
    if same_file:
        return same_file
    same_dir = [sid for sid, f, _ in entries if _parent_dir(f) == _parent_dir(file_path)]
    if same_dir:
        return same_dir
    if len(entries) <= HUB_LIMIT:
        return [sid for sid, _, _ in entries]
    return []
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


def _discover_sql_files(repository_path: Path) -> list[Path]:
    """db/schemas/*.sql + any alter_schema.sql — the DDL and the migration
    history the graph couldn't see (column drops, ref_id format changes)."""
    out: list[Path] = []
    schemas_dir = repository_path / "db" / "schemas"
    if schemas_dir.is_dir():
        out.extend(sorted(p for p in schemas_dir.glob("*.sql") if p.is_file()))
    out.extend(sorted(
        p for p in repository_path.rglob("alter_schema.sql")
        if p.is_file() and ".git" not in p.parts
    ))
    seen = set()
    unique = []
    for p in out:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            unique.append(p)
    return unique


def _extract_chunks_for_file(relative_path: str, source: str) -> tuple[list[CodeChunk], list[GraphEdge]]:
    """Per-file extraction dispatcher — Rust via tree-sitter, SQL via
    sql_schema (tables, columns, migration blocks). Returns (chunks, edges):
    edges are extractor-declared relations (migration touches table, column
    belongs to table) — call/type edges are computed separately."""
    if relative_path.endswith(".sql"):
        from app.rag.ingestion.sql_schema import extract_sql_chunks

        return extract_sql_chunks(relative_path, source)

    chunks = extract_rust_chunks(relative_path, source)
    file_summary = generate_file_summary_chunk(relative_path, source, chunks)
    if file_summary:
        chunks.append(file_summary)
    module_exports = generate_module_exports_chunk(relative_path, source)
    if module_exports:
        chunks.append(module_exports)
    return chunks, []


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


# Mod chunks are containers, not callable units — their content duplicates
# every inner symbol's code, so edges from a mod node are pure phantoms (a
# mod re-export once showed 25 callers and a CRITICAL risk score).
GRAPH_SKIP_KINDS = _SCHEMA_GRAPH_SKIP_KINDS
# SQL symbols ARE graph nodes (searchable, blast-radiused) but their edges are
# declared by the SQL extractor — regex call/type extraction would fabricate
# nonsense from DDL text.
EDGE_EXTRACTION_SKIP_KINDS = GRAPH_SKIP_KINDS | frozenset({"sql_table", "sql_column", "migration_block"})


def _should_skip_for_graph(chunk: CodeChunk) -> bool:
    if chunk.kind in GRAPH_SKIP_KINDS:
        return True
    short_name = _symbol_name(chunk.symbol_id)
    if short_name in ("Relation",):
        return True
    if short_name in ("fn", "let", "match", "if"):
        return True  # parser artifacts — no symbol is legitimately named these
    if short_name == "new" and chunk.kind == "fn" and len(chunk.content.strip()) < 50:
        return True
    return False


def _should_skip_edge_extraction(chunk: CodeChunk) -> bool:
    return chunk.kind in EDGE_EXTRACTION_SKIP_KINDS or _should_skip_for_graph(chunk)


def _extract_call_targets(
    chunk: CodeChunk,
    name_index: dict[str, list[tuple[str, str]]],
    path_index: dict[str, list[str]] | None = None,
) -> list[str]:
    calls: list[str] = []
    # Qualified calls first (Foo::new, Type::method) — precise resolution by
    # path suffix, immune to hub-name ambiguity.
    if path_index is not None:
        for match in QUALIFIED_CALL_PATTERN.finditer(chunk.content):
            qual = match.group(1)
            parts = qual.split("::")
            if len(parts) < 2 or parts[-1] in _RESOLVE_SKIP_NAMES:
                continue
            key = f"{parts[-2]}::{parts[-1]}"
            for symbol_id in path_index.get(key, [])[:HUB_LIMIT]:
                if symbol_id != chunk.symbol_id and symbol_id not in calls:
                    calls.append(symbol_id)
    for match in CALL_PATTERN.finditer(chunk.content):
        candidate = match.group(1)
        if candidate in RUST_KEYWORDS or candidate in RUST_GENERIC_METHODS:
            continue
        if candidate in DERIVE_TRAIT_NAMES:
            continue
        for symbol_id in _resolve_candidates(candidate, chunk.file_path, name_index, kinds=_CALLABLE_KINDS):
            if symbol_id != chunk.symbol_id and symbol_id not in calls:
                calls.append(symbol_id)
    for match in METHOD_CALL_PATTERN.finditer(chunk.content):
        candidate = match.group(1)
        if candidate in RUST_KEYWORDS or candidate in RUST_GENERIC_METHODS:
            continue
        for symbol_id in _resolve_candidates(candidate, chunk.file_path, name_index, kinds=_CALLABLE_KINDS):
            if symbol_id != chunk.symbol_id and symbol_id not in calls:
                calls.append(symbol_id)
    return calls


def _extract_type_references(chunk: CodeChunk, name_index: dict[str, list[tuple[str, str]]]) -> list[str]:
    uses: list[str] = []
    for match in TYPE_REF_PATTERN.finditer(chunk.content):
        candidate = match.group(1) or match.group(2)
        if not candidate:
            continue
        if candidate in RUST_TYPE_KEYWORDS or candidate in RUST_KEYWORDS:
            continue
        if candidate in DERIVE_TRAIT_NAMES:
            continue
        for symbol_id in _resolve_candidates(candidate, chunk.file_path, name_index, kinds=_TYPE_KINDS):
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
        for symbol_id in _resolve_candidates(candidate, chunk.file_path, name_index, kinds=_TYPE_KINDS):
            if symbol_id != chunk.symbol_id and symbol_id not in seen and symbol_id not in chunk.symbol_id.split("::")[:-1]:
                seen.add(symbol_id)
                modes = _classify_usage_modes(chunk.content, candidate)
                results.append((symbol_id, modes))
    return results


def _rebuild_graph_and_semantic_parallel(snapshot: IndexSnapshot, *, force: bool = False) -> dict[str, int]:
    import time as _time
    _build_start = _time.time()

    from app.core.notifications import notify_build_started
    notify_build_started("graph+semantic", total_symbols=len(snapshot.chunks))

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
    try:
        from app.core.prom_metrics import INDEX_REBUILD_IN_PROGRESS
        INDEX_REBUILD_IN_PROGRESS.set(1)
    except Exception:
        pass

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
                (edge.target_symbol_id, edge.usage_modes or ["reference"]))
        else:
            calls_by_source.setdefault(edge.source_symbol_id, []).append(edge.target_symbol_id)

    # SCIP refinement: where a rust-analyzer index is available, its
    # type-resolved edges REPLACE the regex edges for files it covers —
    # precise beats approximate. Uncovered files keep regex edges.
    scip_refinement = _load_scip_refinement(snapshot)
    covered_files: set[str] = set()
    if scip_refinement is not None:
        scip_edges, covered_files, scip_stats = scip_refinement
        replaced_sources = 0
        for chunk in snapshot.chunks:
            if chunk.file_path in covered_files:
                sid = chunk.symbol_id
                if sid in calls_by_source or sid in uses_with_modes_by_source:
                    replaced_sources += 1
                calls_by_source.pop(sid, None)
                uses_with_modes_by_source.pop(sid, None)
                for target_id, relation in scip_edges.get(sid, []):
                    if relation == "calls":
                        calls_by_source.setdefault(sid, []).append(target_id)
                    else:
                        uses_with_modes_by_source.setdefault(sid, []).append((target_id, ["reference"]))
        logger.info(
            "SCIP refinement: %d files covered, %d sources switched to type-resolved edges "
            "(%d calls, %d uses from SCIP)",
            len(covered_files), replaced_sources, scip_stats.get("edges_calls", 0),
            scip_stats.get("edges_uses", 0),
        )

    chunk_meta: dict[str, dict] = {}
    for chunk in snapshot.chunks:
        chunk_meta[chunk.symbol_id] = {
            "kind": chunk.kind,
            "file_path": chunk.file_path,
            "start_line": chunk.start_line,
            "end_line": chunk.end_line,
            "module": chunk.file_path.removesuffix(".rs").replace("/", "::"),
            "is_test": chunk.is_test,
            "visibility": chunk.visibility,
        }

    for chunk in snapshot.chunks:
        if _should_skip_for_graph(chunk):
            continue
        # covered files' edges come wholly from SCIP (regex was popped above);
        # everything else is extractor-guessed. Per-edge provenance for agents
        # and risk weighting — see get_edge_provenance / get_index_meta.
        new_graph.upsert_symbol(
            chunk.symbol_id,
            calls=calls_by_source.get(chunk.symbol_id, []),
            uses_with_modes=uses_with_modes_by_source.get(chunk.symbol_id, []),
            metadata=chunk_meta.get(chunk.symbol_id),
            edge_source="scip" if chunk.file_path in covered_files else "regex",
        )

    new_stats = new_graph.get_stats()
    logger.info("New graph (gen %d) built: %d nodes, %d edges — swapping", new_gen, new_stats["graph_nodes"], new_stats["graph_edges"])

    old_graph = graph_index.swap(new_graph, build_id=build_id)
    try:
        from app.core.prom_metrics import INDEX_REBUILD_IN_PROGRESS
        INDEX_REBUILD_IN_PROGRESS.set(0)
    except Exception:
        pass

    # New generation active — gen-keyed entries miss anyway, but tagged
    # graph answers (blast radius etc.) from the PREVIOUS gen still occupy
    # memory; drop them explicitly.
    try:
        from app.core.query_cache import invalidate_tags

        invalidate_tags("graph")
    except Exception:
        pass


    def _cleanup_old_gen():
        try:
            new_graph.cleanup_other_gens()
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

    from app.core.notifications import notify_build_completed
    notify_build_completed(
        "graph+semantic",
        graph_nodes=new_stats["graph_nodes"],
        graph_edges=new_stats["graph_edges"],
        semantic_documents=len(snapshot.chunks),
        duration_sec=_time.time() - _build_start,
    )

    return new_stats


def _load_scip_refinement(snapshot) -> tuple | None:
    """(scip_edges, covered_files, stats) if a SCIP index is configured —
    or None when unavailable/disabled."""
    from app.core.config import settings

    path = getattr(settings, "scip_index_path", "") or ""
    enabled = getattr(settings, "scip_refine_enabled", False)
    if not enabled or not path:
        return None
    import os

    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        logger.warning("SCIP refinement enabled but index missing/empty at %s", path)
        return None
    try:
        from app.rag.ingestion.scip_ingestor import build_scip_edges, parse_index

        index = parse_index(path)
        covered = {
            d.relative_path for d in index.documents if d.relative_path.endswith(".rs")
        }
        # stale index (older than the indexed commit) still refines better
        # than regex — but log the mismatch for observability
        edges, stats = build_scip_edges(path, snapshot.chunks)
        return edges, covered, stats
    except Exception:
        logger.warning("SCIP refinement failed — falling back to regex edges", exc_info=True)
        return None



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
    import time as _time
    _start = _time.time()
    try:
        from app.core.prom_metrics import INDEX_REBUILD_IN_PROGRESS
        INDEX_REBUILD_IN_PROGRESS.set(1)
    except Exception:
        pass
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

        from app.core.notifications import notify_build_completed
        notify_build_completed(
            "semantic",
            graph_nodes=graph_index.get_stats()["graph_nodes"],
            graph_edges=graph_index.get_stats()["graph_edges"],
            semantic_documents=len(snapshot.chunks),
            duration_sec=_time.time() - _start,
            trigger="semantic_rebuild",
        )
    except Exception as exc:
        from app.core.notifications import notify_build_failed
        notify_build_failed("semantic", str(exc), trigger="semantic_rebuild")
        logger.exception("Semantic index rebuild failed")
    finally:
        with _semantic_rebuild_lock:
            _semantic_rebuild_in_progress = False
            logger.info("Semantic rebuild complete")
        try:
            from app.core.prom_metrics import INDEX_REBUILD_IN_PROGRESS
            INDEX_REBUILD_IN_PROGRESS.set(0)
        except Exception:
            pass


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


_REBUILD_MUTEX = threading.Lock()


class RebuildInProgressError(RuntimeError):
    """Raised when a graph rebuild is requested while one is already running.

    Concurrent rebuilds corrupt each other: build A's post-swap generation
    cleanup deletes build B's in-flight nodes (observed live on 2026-10-06 —
    three overlapping /api/index/repository calls left a half-built graph
    serving). The API layer maps this to HTTP 409."""


def index_rust_repository(repository_path: str | None = None) -> IndexingResult:
    if not _REBUILD_MUTEX.acquire(blocking=False):
        raise RebuildInProgressError("a graph rebuild is already in progress")
    try:
        return _index_rust_repository_locked(repository_path)
    finally:
        _REBUILD_MUTEX.release()


def _index_rust_repository_locked(repository_path: str | None = None) -> IndexingResult:
    target_path = resolve_repository_path(repository_path)
    rust_files = _discover_rust_files(target_path)
    sql_files = _discover_sql_files(target_path)

    all_chunks: list[CodeChunk] = []
    sql_edges: list[GraphEdge] = []
    name_index: dict[str, list[str]] = {}

    for file_path in rust_files + sql_files:
        relative_path = file_path.relative_to(target_path).as_posix()
        source = file_path.read_text(encoding="utf-8")
        chunks, extractor_edges = _extract_chunks_for_file(relative_path, source)
        all_chunks.extend(chunks)
        sql_edges.extend(extractor_edges)

    name_index = _build_name_index(all_chunks)
    path_index = _build_path_index(all_chunks)

    graph_edges: list[GraphEdge] = []
    for chunk in all_chunks:
        if _should_skip_edge_extraction(chunk):
            continue
        calls = _extract_call_targets(chunk, name_index, path_index)
        graph_edges.extend(
            GraphEdge(source_symbol_id=chunk.symbol_id, target_symbol_id=target_symbol_id, relation="calls")
            for target_symbol_id in calls
        )
        uses_with_modes = _extract_type_references_with_modes(chunk, name_index)
        graph_edges.extend(
            GraphEdge(source_symbol_id=chunk.symbol_id, target_symbol_id=sid, relation="uses", usage_modes=modes)
            for sid, modes in uses_with_modes
        )

    # SQL structure: migration touches table, column belongs to table, and
    # sea-orm entity fields map to same-named columns. Edges targeting nothing
    # that exists are dropped — a phantom target becomes a bare graph node.
    try:
        from app.rag.ingestion.sql_schema import link_sql_to_entities

        sql_edges = list(sql_edges) + link_sql_to_entities(all_chunks)
    except Exception:
        logger.warning("SQL entity linking failed", exc_info=True)
    known_ids = {c.symbol_id for c in all_chunks}
    graph_edges.extend(e for e in sql_edges if e.target_symbol_id in known_ids)

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
        files_indexed=len(rust_files) + len(sql_files),
        chunks=all_chunks,
        graph_edges=graph_edges,
        last_indexed_commit=head_commit,
        symbol_set_hash=compute_symbol_set_hash(all_chunks),
    )
    index_metadata_store.replace_snapshot(snapshot)

    # Metal-detector pass: TODOs, swallowed errors, stubs, commented-out code.
    try:
        from app.rag.ingestion import observation_store
        observation_store.sync_observations(target_path, snapshot.chunks)
    except Exception:
        logger.warning("Observation sync failed (full index)", exc_info=True)

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
    if not _REBUILD_MUTEX.acquire(blocking=False):
        raise RebuildInProgressError("a graph rebuild is already in progress")
    try:
        return _replay_indexes_from_storage_locked()
    finally:
        _REBUILD_MUTEX.release()


def _replay_indexes_from_storage_locked() -> IndexingResult | None:
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

    # Register semantic rebuilds (autobuild path) in build history too.
    try:
        from app.rag.reinforcement import build_registry
        parent = build_registry.get_active_build()
        build_id = build_registry.register_build(
            build_type="semantic",
            parent_build_id=parent["build_id"] if parent else None,
            weaviate_collection=getattr(semantic_index, "get_active_collection_name", lambda: None)(),
            neo4j_gen=None,
        )
        if build_id:
            build_registry.complete_build(
                build_id,
                chunk_count=len(snapshot.chunks),
                graph_nodes=graph_stats["graph_nodes"],
                graph_edges=graph_stats["graph_edges"],
            )
    except Exception:
        logger.warning("Failed to register semantic build", exc_info=True)

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
    if not _REBUILD_MUTEX.acquire(blocking=False):
        raise RebuildInProgressError("a graph rebuild is already in progress — retry the incremental sync after it completes")
    try:
        return _incremental_update_symbols_locked(modified_files, repository_path)
    finally:
        _REBUILD_MUTEX.release()


def _incremental_update_symbols_locked(
    modified_files: list[str],
    repository_path: str | None = None,
) -> IndexingResult:
    target_path = resolve_repository_path(repository_path)
    snapshot = index_metadata_store.load_snapshot()
    if snapshot is None:
        return index_rust_repository(repository_path)

    existing_chunks = list(snapshot.chunks)
    existing_edges = list(snapshot.graph_edges)
    
    modified_symbol_ids: set[str] = set()
    # (symbol_id, file_path) of symbols deleted from source — the pair is the
    # unit of removal so a pathless id shared by two files loses only the row
    # for the file it disappeared from.
    removed_symbols: set[tuple[str, str]] = set()
    removed_files: list[str] = []
    new_chunks: list[CodeChunk] = []
    incremental_sql_edges: list[GraphEdge] = []

    for file_path_str in modified_files:
        absolute_path = target_path / file_path_str
        old_ids = {c.symbol_id for c in existing_chunks if c.file_path == file_path_str}
        if not absolute_path.exists():
            # File deleted from source — everything it owned goes with it.
            removed_symbols |= {(sid, file_path_str) for sid in old_ids}
            removed_files.append(file_path_str)
            existing_chunks = [chunk for chunk in existing_chunks if chunk.file_path != file_path_str]
            continue

        source = absolute_path.read_text(encoding="utf-8")
        file_chunks, extractor_edges = _extract_chunks_for_file(file_path_str, source)
        new_ids = {c.symbol_id for c in file_chunks}
        # Symbols that lived in this file but no longer exist (renamed or
        # deleted) — the source of the stale-symbol bug this replaces.
        removed_symbols |= {(sid, file_path_str) for sid in old_ids - new_ids}
        for chunk in file_chunks:
            modified_symbol_ids.add(chunk.symbol_id)
            new_chunks.append(chunk)
        incremental_sql_edges.extend(extractor_edges)

        existing_chunks = [chunk for chunk in existing_chunks if chunk.file_path != file_path_str]

    removed_ids = {sid for sid, _ in removed_symbols}
    all_chunks = existing_chunks + new_chunks
    # Drop every edge touching a removed symbol; recompute outgoing edges of
    # modified symbols below. Edges INTO modified symbols survive (their
    # callers didn't change).
    gone_sources = modified_symbol_ids | removed_ids
    existing_edges = [
        edge for edge in existing_edges
        if edge.source_symbol_id not in gone_sources and edge.target_symbol_id not in removed_ids
    ]
    name_index = _build_name_index(all_chunks)
    path_index = _build_path_index(all_chunks)

    new_edges: list[GraphEdge] = []
    for chunk in new_chunks:
        if _should_skip_edge_extraction(chunk):
            continue
        calls = _extract_call_targets(chunk, name_index, path_index)
        new_edges.extend(
            GraphEdge(source_symbol_id=chunk.symbol_id, target_symbol_id=target_id, relation="calls")
            for target_id in calls
        )
        uses_with_modes = _extract_type_references_with_modes(chunk, name_index)
        new_edges.extend(
            GraphEdge(source_symbol_id=chunk.symbol_id, target_symbol_id=sid, relation="uses", usage_modes=modes)
            for sid, modes in uses_with_modes
        )
    # Phantom-target guard for SQL extractor edges only — an edge to a
    # non-existent sql::column::t::c would become a bare graph node. Rust
    # call edges to unresolved callees are legitimate scaffolding and keep
    # their bare targets.
    all_known_ids = {c.symbol_id for c in all_chunks}
    new_edges.extend(e for e in incremental_sql_edges if e.target_symbol_id in all_known_ids)

    removed_edge_keys = [
        (e.source_symbol_id, e.target_symbol_id)
        for e in snapshot.graph_edges
        if e.source_symbol_id in gone_sources or e.target_symbol_id in removed_ids
    ]

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

    index_metadata_store.update_incremental(
        modified_chunks=new_chunks,
        removed_symbols=sorted(removed_symbols),
        modified_edges=new_edges,
        removed_edge_keys=removed_edge_keys,
        last_indexed_commit=head_commit,
        symbol_set_hash=compute_symbol_set_hash(all_chunks),
    )

    # Delete symbols that vanished from source — upsert-only updates left
    # lookup_txn_for_advice-style ghosts in the graph and Weaviate forever.
    if removed_ids:
        graph_index.remove_symbols(sorted(removed_ids))
        semantic_index.delete_chunks(sorted(removed_ids))
    for gone_file in removed_files:
        try:
            from app.rag.ingestion import observation_store as _obs
            _obs.replace_file_observations(gone_file, [])
        except Exception:
            logger.warning("Observation cleanup failed for %s", gone_file, exc_info=True)

    # Metal-detector pass for the changed files only.
    try:
        from app.rag.ingestion import observation_store
        observation_store.sync_observations(
            target_path, list(snapshot.chunks), files=[str(p) for p in modified_files]
        )
    except Exception:
        logger.warning("Observation sync failed (incremental)", exc_info=True)

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
        if _should_skip_for_graph(chunk):
            continue
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
                "is_test": chunk.is_test,
                "visibility": chunk.visibility,
            },
        )

    graph_stats = graph_index.get_stats()

    # Register the incremental refresh so build history reflects reality —
    # previously only full rebuilds created registry rows, leaving the
    # dashboard's active build stale after nightly syncs.
    try:
        from app.rag.reinforcement import build_registry
        parent = build_registry.get_active_build()
        build_id = build_registry.register_build(
            build_type="incremental",
            parent_build_id=parent["build_id"] if parent else None,
            weaviate_collection=getattr(semantic_index, "get_active_collection_name", lambda: None)(),
            neo4j_gen=None,
            metadata={"modified_files": len(modified_files), "symbols_updated": len(new_chunks)},
        )
        if build_id:
            replaced = sum(1 for c in existing_chunks if c.symbol_id in modified_symbol_ids)
            build_registry.complete_build(
                build_id,
                chunk_count=len(existing_chunks) - replaced + len(new_chunks),
                graph_nodes=graph_stats["graph_nodes"],
                graph_edges=graph_stats["graph_edges"],
            )
    except Exception:
        logger.warning("Failed to register incremental build", exc_info=True)

    return IndexingResult(
        symbols_indexed=len(new_chunks),
        semantic_documents=len(new_chunks),
        graph_nodes=graph_stats["graph_nodes"],
        graph_edges=graph_stats["graph_edges"],
        files_indexed=len(modified_files),
        repository_path=str(target_path),
    )