from pathlib import Path
import subprocess
from uuid import uuid4

from app.rag.indexing_service import incremental_update_symbols, resolve_repository_path
from app.rag.retrieval.graph import graph_index
from app.schemas.kb_sync import KbSyncAcceptedResponse, KbSyncResult
from app.tasks.celery_app import celery_app
from app.tasks.workers.notifications import publish_notification


def _git_diff_rust_files(repository_path: Path, old_commit: str, new_commit: str) -> list[str]:
    completed = subprocess.run(
        ["git", "diff", "--name-only", old_commit, new_commit, "--", "*.rs"],
        cwd=repository_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git diff failed")
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _extract_modified_symbols(repository_path: Path, modified_files: list[str]) -> list[str]:
    from app.rag.ingestion.tree_sitter import extract_rust_chunks

    modified_symbols: list[str] = []
    for file_path in modified_files:
        absolute_path = repository_path / file_path
        if not absolute_path.exists():
            continue
        source = absolute_path.read_text(encoding="utf-8")
        for chunk in extract_rust_chunks(file_path, source):
            if chunk.symbol_id not in modified_symbols:
                modified_symbols.append(chunk.symbol_id)
    return modified_symbols


def process_kb_sync(old_commit: str, new_commit: str, repository_path: str | None = None) -> KbSyncResult:
    target_path = resolve_repository_path(repository_path)
    modified_files = _git_diff_rust_files(target_path, old_commit, new_commit)
    
    indexing_result = incremental_update_symbols(modified_files, str(target_path))
    
    modified_symbols = _extract_modified_symbols(target_path, modified_files)

    impacted_symbols: list[str] = []
    for symbol_id in modified_symbols:
        for neighborhood in graph_index.traverse(symbol_id, depth=2):
            for candidate in neighborhood.upstream + neighborhood.downstream:
                if candidate not in modified_symbols and candidate not in impacted_symbols:
                    impacted_symbols.append(candidate)

    result = KbSyncResult(
        status="completed",
        old_commit=old_commit,
        new_commit=new_commit,
        repository_path=str(target_path),
        modified_files=modified_files,
        modified_symbols=modified_symbols,
        impacted_symbols=impacted_symbols,
        files_indexed=indexing_result.files_indexed,
        graph_edges=indexing_result.graph_edges,
    )
    publish_notification("sync_complete", result.model_dump(mode="json"))
    return result


@celery_app.task(name="kb_sync.process")
def process_kb_sync_task(old_commit: str, new_commit: str, repository_path: str | None = None) -> dict[str, object]:
    return process_kb_sync(old_commit, new_commit, repository_path).model_dump(mode="json")


def enqueue_kb_sync(old_commit: str, new_commit: str, repository_path: str | None = None) -> KbSyncAcceptedResponse:
    if celery_app.conf.task_always_eager:
        result = process_kb_sync(old_commit, new_commit, repository_path)
        result.task_id = uuid4().hex
        return KbSyncAcceptedResponse(
            task_id=result.task_id,
            status="completed",
            old_commit=old_commit,
            new_commit=new_commit,
        )

    task = process_kb_sync_task.apply_async(args=[old_commit, new_commit, repository_path])
    return KbSyncAcceptedResponse(
        task_id=task.id,
        status="queued",
        old_commit=old_commit,
        new_commit=new_commit,
    )