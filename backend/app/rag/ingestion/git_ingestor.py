"""Incremental git ingestor — detects new commits and reindexes only changed files + their dependents."""

import logging
import subprocess
from pathlib import Path

from app.rag.indexing_service import incremental_update_symbols, resolve_repository_path
from app.rag.retrieval.graph import graph_index
from app.core.index_store import index_metadata_store

logger = logging.getLogger(__name__)


def get_current_head(repository_path: str | None = None) -> str:
    repo_path = resolve_repository_path(repository_path)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git rev-parse failed: {result.stderr.strip()}")
    return result.stdout.strip()


def get_last_indexed_commit() -> str:
    snapshot = index_metadata_store.load_snapshot()
    return snapshot.last_indexed_commit if snapshot else ""


def get_changed_files(from_commit: str, repository_path: str | None = None) -> list[str]:
    repo_path = resolve_repository_path(repository_path)
    if not from_commit:
        return []
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{from_commit}..HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed: {result.stderr.strip()}")
    changed = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return [f for f in changed if f.endswith(".rs")]


def get_deleted_files(from_commit: str, repository_path: str | None = None) -> list[str]:
    repo_path = resolve_repository_path(repository_path)
    if not from_commit:
        return []
    result = subprocess.run(
        ["git", "diff", "--diff-filter=D", "--name-only", f"{from_commit}..HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip() and line.strip().endswith(".rs")]


def _expand_with_dependents(changed_files: list[str]) -> list[str]:
    """Walk the graph to find all files that depend on changed files.

    If File B changes, File A (which calls or uses B) must also be re-embedded
    because its semantic context has changed. Without this, a Sev-1 incident in
    File A would retrieve stale vectors.
    """
    snapshot = index_metadata_store.load_snapshot()
    if snapshot is None:
        return changed_files

    changed_set = set(changed_files)

    symbol_to_file: dict[str, str] = {}
    changed_symbols: list[str] = []
    for chunk in snapshot.chunks:
        symbol_to_file[chunk.symbol_id] = chunk.file_path
        if chunk.file_path in changed_set:
            changed_symbols.append(chunk.symbol_id)

    dependent_files: set[str] = set()
    for symbol_id in changed_symbols:
        neighborhood = graph_index.get_blast_radius(symbol_id)
        for caller in neighborhood.upstream + neighborhood.used_by:
            caller_file = symbol_to_file.get(caller)
            if caller_file and caller_file not in changed_set:
                dependent_files.add(caller_file)

    if dependent_files:
        logger.info(
            "Blast-radius expansion: %d changed files + %d dependent files = %d total",
            len(changed_files), len(dependent_files), len(changed_files) + len(dependent_files),
        )

    return sorted(changed_set | dependent_files)


def ingest_incremental(repository_path: str | None = None) -> dict[str, object]:
    last_commit = get_last_indexed_commit()
    if not last_commit:
        logger.info("No previous indexed commit — running full reindex")
        try:
            from app.rag.indexing_service import index_rust_repository
            repo_path = resolve_repository_path(repository_path)
            result = index_rust_repository(str(repo_path))
            head = get_current_head(repository_path)
            return {
                "status": "full_reindex_complete",
                "symbols_indexed": result.symbols_indexed,
                "files_indexed": result.files_indexed,
                "graph_nodes": result.graph_nodes,
                "graph_edges": result.graph_edges,
                "head_commit": head,
            }
        except Exception as exc:
            logger.error("Full reindex failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    try:
        head_commit = get_current_head(repository_path)
    except Exception as exc:
        return {"status": "error", "error": str(exc)}

    if head_commit == last_commit:
        return {"status": "up_to_date", "last_commit": last_commit, "head_commit": head_commit}

    directly_changed = get_changed_files(last_commit, repository_path)
    deleted = get_deleted_files(last_commit, repository_path)

    if not directly_changed and not deleted:
        _update_last_commit(head_commit)
        return {"status": "up_to_date", "last_commit": last_commit, "head_commit": head_commit}

    changed = _expand_with_dependents(directly_changed)

    logger.info(
        "Incremental ingest: %d directly changed + dependents = %d total, %d deleted (from %s to %s)",
        len(directly_changed), len(changed), len(deleted), last_commit[:8], head_commit[:8],
    )

    from app.core.notifications import notify_build_started, notify_build_completed, notify_build_failed
    import time as _time
    _start = _time.time()

    notify_build_started(
        "incremental",
        total_symbols=len(changed) * 15,  # rough estimate: ~15 symbols per file
        from_commit=last_commit,
        to_commit=head_commit,
        files_changed=len(directly_changed),
        trigger="nightly_sync",
    )

    try:
        if changed:
            result = incremental_update_symbols(changed, repository_path)
            logger.info(
                "Incremental reindex complete: %d symbols, %d edges",
                result.symbols_indexed, result.graph_edges,
            )
            notify_build_completed(
                "incremental",
                graph_nodes=result.graph_nodes,
                graph_edges=result.graph_edges,
                semantic_documents=result.symbols_indexed,
                duration_sec=_time.time() - _start,
                from_commit=last_commit,
                to_commit=head_commit,
                trigger="nightly_sync",
            )
    except Exception as exc:
        notify_build_failed("incremental", str(exc), trigger="nightly_sync")
        raise

    for deleted_file in deleted:
        _remove_file_chunks(deleted_file)

    _update_last_commit(head_commit)

    return {
        "status": "incremental_update",
        "from_commit": last_commit,
        "to_commit": head_commit,
        "directly_changed_files": directly_changed,
        "dependent_files": sorted(set(changed) - set(directly_changed)),
        "total_reindexed": len(changed),
        "deleted_files": deleted,
    }


def _update_last_commit(commit: str) -> None:
    snapshot = index_metadata_store.load_snapshot()
    if snapshot is None:
        return
    updated = snapshot.model_copy(update={"last_indexed_commit": commit})
    index_metadata_store.replace_snapshot(updated)


def _remove_file_chunks(file_path: str) -> None:
    snapshot = index_metadata_store.load_snapshot()
    if snapshot is None:
        return
    updated_chunks = [c for c in snapshot.chunks if c.file_path != file_path]
    updated_edges = [e for e in snapshot.graph_edges if not e.source_symbol_id.startswith(file_path)]
    if len(updated_chunks) == len(snapshot.chunks):
        return
    updated = snapshot.model_copy(update={
        "chunks": updated_chunks,
        "graph_edges": updated_edges,
        "files_indexed": max(0, snapshot.files_indexed - 1),
    })
    index_metadata_store.replace_snapshot(updated)
