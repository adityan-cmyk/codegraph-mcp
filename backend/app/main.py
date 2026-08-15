import json
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.agents.client import hosted_model_client
from app.api.routers.chat import router as chat_router
from app.api.routers.eval import router as eval_router
from app.api.routers.feedback import router as feedback_router
from app.api.routers.graph import router as graph_router
from app.api.routers.indexing import router as indexing_router
from app.api.routers.incidents import router as incidents_router
from app.api.routers.kb_sync import router as kb_sync_router
from app.api.websockets.incidents import incident_room_socket
from app.core.config import settings
from app.core.health import check_all_backends
from app.core.health import check_readiness
from app.core.learning_service import replay_resolved_errors_from_storage
from app.core.metrics import metrics_collector
from app.core.state_machine import IncidentState
from app.core.tracing_service import tracing_service
from app.rag.indexing_service import index_rust_repository, replay_indexes_from_storage
from app.schemas.incident import ModelChatRequest, ModelChatResponse

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    import subprocess
    try:
        subprocess.run(
            ["git", "config", "--global", "--add", "safe.directory", settings.codebase_root_path or ""],
            capture_output=True,
            check=False,
            timeout=5,
        )
    except Exception:
        pass

    if settings.index_on_startup and settings.codebase_root_path:
        logger.info("Auto-indexing repository on startup: %s", settings.codebase_root_path)
        import asyncio
        asyncio.get_event_loop().run_in_executor(None, index_rust_repository, None)
    elif settings.index_replay_on_startup:
        replay_indexes_from_storage()
        replay_resolved_errors_from_storage()

    import threading
    import uvicorn
    from app.mcp.readonly_server import readonly_mcp_server

    def _run_readonly_mcp():
        config = uvicorn.Config(
            app=readonly_mcp_server,
            host=settings.readonly_mcp_host,
            port=settings.readonly_mcp_port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        server.run()

    mcp_thread = threading.Thread(target=_run_readonly_mcp, daemon=True, name="readonly-mcp")
    mcp_thread.start()
    logger.info(
        "Read-only MCP server (stateless) started on %s:%d/mcp",
        settings.readonly_mcp_host,
        settings.readonly_mcp_port,
    )

    try:
        from app.rag.reinforcement.agent import start_agent as start_reinforcement_agent
        start_reinforcement_agent()
    except Exception:
        logger.warning("Failed to start reinforcement agent", exc_info=True)

    yield
    await hosted_model_client.close()


app = FastAPI(title="On-call Assistant API", lifespan=lifespan)

from app.core.auth import AuthMiddleware
from app.core.health import check_all_backends, check_readiness
from app.core.rate_limit import RateLimitMiddleware
from app.core.structured_logging import TraceIdMiddleware, setup_structured_logging

setup_structured_logging()

app.add_middleware(RateLimitMiddleware)
app.add_middleware(TraceIdMiddleware)
app.add_middleware(AuthMiddleware)

app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Accept", "Authorization", "Content-Type"],
)

app.include_router(incidents_router)
app.include_router(indexing_router)
app.include_router(eval_router)
app.include_router(kb_sync_router)
app.include_router(chat_router)
app.include_router(graph_router)
app.include_router(feedback_router)
app.add_api_websocket_route("/ws/incident/{session_id}", incident_room_socket)


@app.get("/health")
def healthcheck() -> dict[str, object]:
    return {"status": "ok", "default_state": IncidentState.CREATED.value}


@app.get("/ready")
def readiness_check() -> dict[str, object]:
    return check_readiness()


@app.get("/api/health")
def detailed_health() -> dict[str, object]:
    return check_all_backends()


@app.get("/api/metrics")
def get_metrics() -> dict[str, object]:
    return metrics_collector.snapshot()


@app.get("/api/tracing/status")
def tracing_status() -> dict[str, object]:
    return {"enabled": tracing_service.is_enabled, "backend": "langfuse" if tracing_service.is_enabled else None}


@app.get("/api/model/status")
async def model_status() -> dict[str, object]:
    try:
        connected = await hosted_model_client.test_connection()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Model connectivity check failed: {exc}",
        ) from exc

    return {
        "connected": connected,
        "model": settings.litellm_model,
        "base_url": settings.litellm_base_url,
    }


_FILLER_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "about", "like",
    "through", "after", "over", "between", "out", "against", "during",
    "without", "before", "under", "around", "among", "and", "but", "or",
    "nor", "not", "so", "yet", "both", "either", "neither", "each",
    "every", "all", "any", "few", "more", "most", "other", "some",
    "such", "no", "only", "own", "same", "than", "too", "very",
    "just", "because", "if", "when", "where", "how", "what", "which",
    "who", "whom", "this", "that", "these", "those", "i", "me", "my",
    "we", "our", "you", "your", "he", "him", "his", "she", "her",
    "it", "its", "they", "them", "their", "please", "tell", "know",
    "give", "show", "explain", "help", "want", "need", "get", "let",
    "also", "whatever", "whatevre", "etc", "well", "basically",
    "actually", "really", "much", "many", "thing", "things",
    "bout", "abt", "pls", "thx", "tho", "kinda",
})


def _extract_title_from_message(message: str) -> str:
    cleaned = message.strip().replace("\n", " ")
    for ch in "?!.,;:":
        cleaned = cleaned.replace(ch, " ")
    words = cleaned.split()
    key_words = [w for w in words if w.lower() not in _FILLER_WORDS]
    if not key_words:
        key_words = words[:6]
    title = " ".join(key_words[:8])
    if len(title) > 60:
        title = title[:57] + "..."
    return title


@app.post("/api/model/chat", response_model=ModelChatResponse)
async def model_chat(payload: ModelChatRequest) -> ModelChatResponse:
    metrics_collector.record_chat_request()

    from app.rag.retrieval.semantic import semantic_index
    from app.rag.retrieval.graph import get_blast_radius
    from app.schemas.incident import Citation
    import json

    citations: list[Citation] = []
    codebase_context = ""
    directory_context = ""
    try:
        from app.rag.indexing_service import _build_directory_tree
        from app.core.config import settings
        from pathlib import Path
        repo_path = settings.codebase_root_path
        if repo_path:
            try:
                directory_context = _build_directory_tree(Path(repo_path))
            except Exception:
                logger.debug("Failed to build directory tree", exc_info=True)
    except Exception:
        logger.debug("Failed to load repository context", exc_info=True)

    try:
        matches = semantic_index.query_chunks(payload.message, limit=50)
        query_tokens = set(re.findall(r"[a-zA-Z]+", payload.message.lower()))
        boosted = []
        for m in matches:
            score = m.score
            if m.symbol_id.endswith("::file_summary"):
                score = min(score * 1.6, 1.0)
            elif m.symbol_id.endswith("::module_exports"):
                score = score * 0.2
            # Path keyword overlap boost
            path_tokens = set(re.findall(r"[a-zA-Z]+", m.source.lower()))
            overlap = query_tokens & path_tokens
            if overlap:
                # Stronger per-token boost
                score = min(score * (1.0 + 0.25 * len(overlap)), 1.0)
            # Exact filename-stem bonus
            filename_stem = Path(m.source).stem.lower()
            stem_tokens = set(re.findall(r"[a-zA-Z]+", filename_stem))
            stem_overlap = query_tokens & stem_tokens
            if stem_overlap:
                score = min(score * (1.0 + 0.20 * len(stem_overlap)), 1.0)
            boosted.append(m.model_copy(update={"score": score}))
        boosted.sort(key=lambda m: m.score, reverse=True)

        # Deduplicate by file path: keep only top chunk per file
        seen_files = set()
        deduped = []
        for m in boosted:
            if m.source in seen_files:
                continue
            seen_files.add(m.source)
            deduped.append(m)
        matches = deduped[:25]

        logger.info(
            "RAG query=%r returned %d matches (top scores: %s)",
            payload.message[:80],
            len(matches),
            ", ".join(f"{m.score:.3f} {m.symbol_id[:50]}" for m in matches[:5]),
        )

        if matches:
            # Relaxed directory diversity: allow more files from same dir when scores are high
            dir_counts: dict[str, int] = {}
            matched_files: dict[str, list] = {}
            related_symbols: list[str] = []
            max_files = 8
            for match in matches:
                if match.score < 0.40:
                    continue
                match_dir = str(Path(match.source).parent)
                same_dir_count = dir_counts.get(match_dir, 0)
                if match.score >= 0.75:
                    limit_per_dir = 99
                elif match.score >= 0.55:
                    limit_per_dir = 4
                elif match.score >= 0.45:
                    limit_per_dir = 2
                else:
                    limit_per_dir = 1
                if same_dir_count >= limit_per_dir:
                    continue
                dir_counts[match_dir] = same_dir_count + 1
                matched_files.setdefault(match.source, []).append(match)
                related_symbols.append(match.symbol_id)
                citations.append(Citation(
                    symbol_id=match.symbol_id,
                    file_path=match.source,
                    score=match.score,
                    snippet=match.content[:300],
                ))
                if len(matched_files) >= max_files:
                    break

            context_parts = []
            loaded_files: set[str] = set()
            budget_used = 0
            total_budget = 120_000
            repo_path = settings.codebase_root_path

            sorted_files = sorted(matched_files.items(), key=lambda item: item[1][0].score, reverse=True)

            for file_path, file_matches in sorted_files:
                if budget_used >= total_budget:
                    break
                if file_path in loaded_files:
                    continue
                loaded_files.add(file_path)

                if repo_path:
                    full_path = Path(repo_path) / file_path
                    if full_path.exists():
                        file_content = full_path.read_text(encoding="utf-8", errors="replace")
                        remaining = total_budget - budget_used
                        top_score = file_matches[0].score
                        is_high_priority = top_score >= 0.6

                        if not is_high_priority and len(file_content) > remaining:
                            cutoff = max(remaining - 30, 0)
                            file_content = file_content[:cutoff] + "\n// ... (truncated due to context budget)"
                        elif is_high_priority and len(file_content) > remaining:
                            pass

                        context_parts.append(
                            f"// File: {file_path} (matched symbols: {', '.join(m.symbol_id.split('::')[-1] for m in file_matches[:4])})\n```rust\n{file_content}\n```"
                        )
                        budget_used += len(file_content)
                        continue

                for m in file_matches:
                    chunk_text = f"[{m.symbol_id}] (score: {m.score:.3f}, file: {m.source}):\n```\n{m.content[:2000]}\n```"
                    context_parts.append(chunk_text)
                    budget_used += len(chunk_text)

            if budget_used < total_budget * 0.7 and repo_path:
                loaded_dirs: set[str] = set()
                for file_path, file_matches in sorted_files:
                    if budget_used >= total_budget or len(loaded_files) >= 6:
                        break
                    parent_dir = str(Path(file_path).parent)
                    if parent_dir in loaded_dirs:
                        continue
                    loaded_dirs.add(parent_dir)
                    parent_path = Path(repo_path) / parent_dir
                    if not parent_path.exists():
                        continue
                    sibling_files = sorted(
                        p for p in parent_path.glob("*.rs")
                        if str(p.relative_to(repo_path)) not in loaded_files
                    )
                    top_score = file_matches[0].score
                    max_siblings = 2 if top_score >= 0.8 else 1
                    for sibling in sibling_files[:max_siblings]:
                        if budget_used >= total_budget or len(loaded_files) >= 6:
                            break
                        sibling_rel = str(sibling.relative_to(repo_path))
                        if sibling_rel in loaded_files:
                            continue
                        sibling_tokens = set(re.findall(r"[a-zA-Z]+", sibling_rel.lower()))
                        if not (query_tokens & sibling_tokens):
                            continue
                        loaded_files.add(sibling_rel)
                        sibling_content = sibling.read_text(encoding="utf-8", errors="replace")
                        remaining = total_budget - budget_used
                        if len(sibling_content) > remaining:
                            cutoff = max(remaining - 30, 0)
                            sibling_content = sibling_content[:cutoff] + "\n// ... (truncated due to context budget)"
                        context_parts.append(
                            f"// File: {sibling_rel} (sibling of {file_path})\n```rust\n{sibling_content}\n```"
                        )
                        budget_used += len(sibling_content)

            neighborhoods = []
            graph_neighbor_files: dict[str, list[str]] = {}
            for symbol_id in related_symbols[:6]:
                try:
                    neighborhood = get_blast_radius(symbol_id)
                    if neighborhood.upstream or neighborhood.downstream or neighborhood.used_by or neighborhood.uses:
                        neighborhoods.append(neighborhood)
                    all_neighbors = (
                        neighborhood.upstream + neighborhood.downstream
                        + neighborhood.uses + neighborhood.used_by
                    )
                    for neighbor_id in all_neighbors:
                        chunk = semantic_index.get_chunk(neighbor_id)
                        if chunk and chunk.file_path not in loaded_files:
                            graph_neighbor_files.setdefault(chunk.file_path, []).append(neighbor_id)
                        elif not chunk and repo_path:
                            parts = neighbor_id.split("::")
                            for i in range(len(parts) - 1, 0, -1):
                                module_path = "/".join(parts[:i])
                                for prefix in ("src/", ""):
                                    candidate = Path(repo_path) / f"{prefix}{module_path}.rs"
                                    rel = str(candidate.relative_to(repo_path))
                                    if candidate.exists() and rel not in loaded_files:
                                        rel_tokens = set(re.findall(r"[a-zA-Z]+", rel.lower()))
                                        if query_tokens & rel_tokens:
                                            graph_neighbor_files.setdefault(rel, []).append(neighbor_id)
                                        break
                                else:
                                    continue
                                break
                except Exception:
                    pass

            if graph_neighbor_files and budget_used < total_budget * 0.9 and repo_path and len(loaded_files) < 6:
                for file_path, neighbor_ids in sorted(
                    graph_neighbor_files.items(),
                    key=lambda item: len(item[1]),
                    reverse=True,
                ):
                    if budget_used >= total_budget:
                        break
                    if file_path in loaded_files:
                        continue
                    loaded_files.add(file_path)
                    full_path = Path(repo_path) / file_path
                    if not full_path.exists():
                        continue
                    file_content = full_path.read_text(encoding="utf-8", errors="replace")
                    remaining = total_budget - budget_used
                    if len(file_content) > remaining:
                        cutoff = max(remaining - 30, 0)
                        file_content = file_content[:cutoff] + "\n// ... (truncated due to context budget)"
                    context_parts.append(
                        f"// File: {file_path} (graph-connected: {', '.join(n.split('::')[-1] for n in neighbor_ids[:4])})\n```rust\n{file_content}\n```"
                    )
                    budget_used += len(file_content)

            graph_text = ""
            if neighborhoods:
                graph_lines = []
                for n in neighborhoods:
                    up = ", ".join(n.upstream[:8]) if n.upstream else "none"
                    down = ", ".join(n.downstream[:8]) if n.downstream else "none"
                    used_by = ", ".join(n.used_by[:8]) if n.used_by else "none"
                    uses = ", ".join(n.uses[:8]) if n.uses else "none"
                    graph_lines.append(
                        f"  - {n.symbol_id}:\n"
                        f"    calls: [{down}]\n"
                        f"    called by: [{up}]\n"
                        f"    uses (types): [{uses}]\n"
                        f"    used by (as type): [{used_by}]"
                    )
                graph_text = "\nCall & type dependency graph:\n" + "\n".join(graph_lines)

            codebase_context = (
                "\n\nRelevant code from the indexed repository:\n"
                + "\n".join(context_parts)
                + graph_text
            )
    except Exception:
        logger.warning("RAG retrieval failed", exc_info=True)

    has_strong_context = bool(context_parts) and any(
        m.symbol_id.endswith("::file_summary") or m.score >= 0.6
        for m in matches[:3]
    ) if matches else False

    system_content = (
        "You are an on-call assistant for developers working on a Rust codebase. "
        "Be concise and practical.\n\n"
        "RULES:\n"
        "1. For questions about the codebase, use the code context provided below. "
        "When referencing code, just mention the symbol name and file path in plain text (e.g. 'InventoryClient in crates/inventory/client.rs'). "
        "Do NOT use markdown links like [text](url) in your response — they break the UI.\n"
        "2. Explain the WHAT and WHY, not just the code structure. When someone asks about a component, explain what business problem it solves, "
        "what domain concepts it represents, and how it fits into the larger system — not just its fields and method signatures. "
        "Use concrete examples where possible.\n"
        "3. NEVER fabricate, invent, or guess code, symbol names, file paths, or behavior that is not in the provided context or your previous messages in this conversation. "
        "If you are unsure whether something is in the context, it is not — say you don't know.\n"
        "3. For conversational follow-ups (e.g. 'what did I ask?', 'summarize what you said', 'can you explain that differently?'), "
        "use the chat history above. You do NOT need new code context for these.\n"
        "4. If a question is about the codebase and the provided context is unrelated or missing, say you don't have enough context "
        "and list what files or symbols would be needed.\n"
        "5. Do not speculate about what code might look like. Only describe what is actually shown in the context or your prior answers.\n"
    )
    if not has_strong_context and not codebase_context:
        system_content += (
            "\n\nNOTE: No relevant code context was retrieved for this query. "
            "If the question is about the codebase, say you don't have enough context. "
            "If it's a conversational follow-up, answer from the chat history above.\n"
        )
    if directory_context:
        system_content += f"\nRepository directory structure:\n{directory_context}\n"
    if codebase_context:
        system_content += codebase_context
    try:
        from app.rag.indexing_service import semantic_rebuild_is_in_progress
        if semantic_rebuild_is_in_progress():
            system_content += (
                "\n\nWARNING: The codebase index is currently being rebuilt. "
                "The search results below are INCOMPLETE and may be missing the most relevant code. "
                "If the context below does not answer the question, you MUST say you don't have enough context "
                "and that indexing is still in progress. Do NOT attempt to answer without proper context."
            )
    except Exception:
        logger.debug("Failed to check semantic rebuild status", exc_info=True)

    with tracing_service.generation(
        "model_chat",
        model=settings.litellm_model,
        input={"message": payload.message},
    ):
        try:
            messages = [{"role": "system", "content": system_content}]
            for msg in payload.history[-20:]:
                if msg.role == "user" and msg.content == payload.message:
                    continue
                messages.append({"role": msg.role, "content": msg.content})
            messages.append({"role": "user", "content": payload.message})
            reply = await hosted_model_client.chat(messages)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Model chat failed: {exc}",
            ) from exc

    if payload.thread_id:
        try:
            from app.core.repositories.chat_repository import ChatRepository
            chat_repo = ChatRepository(settings.postgres_dsn)
            chat_repo.add_message(payload.thread_id, "user", payload.message)
            citation_dicts = [c.model_dump() for c in citations]
            chat_repo.add_message(payload.thread_id, "assistant", reply, citations=citation_dicts)
            existing_messages = chat_repo.get_messages(payload.thread_id)
            if len(existing_messages) <= 2:
                title = _extract_title_from_message(payload.message)
                chat_repo.rename_thread(payload.thread_id, title)
        except Exception:
            logger.exception("Failed to persist chat messages for thread %s", payload.thread_id)

    return ModelChatResponse(reply=reply, model=settings.litellm_model, citations=citations)
