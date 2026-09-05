# On-Call Assistant (Full Stack)

AI-powered incident triage system with semantic code search, graph-based dependency analysis, root cause detection, LLM patch generation, and a reinforcement loop that improves search quality from AI agent feedback — for Rust codebases.

> **Branch note:** this is the `oncall-assistant` branch — the complete product including
> the incident lifecycle, LLM agent, chat, eval suites, and Celery workers.
> The [`main`](../../tree/main) branch is a stripped-down MCP-only server without these features.

## Architecture (full stack)

```
                    ┌────────────────────────────────────────────────┐
                    │                   FastAPI :8000                │
                    ├────────────────────────────────────────────────┤
 /api/incidents ───▶│ Incident lifecycle (NEW→ANALYZED→PATCHED→     │
                    │                     RESOLVED) + websockets    │
 /api/model/chat ──▶│ RAG chat (semantic+graph context → LiteLLM →  │
                    │   answer with citations, thread persistence)  │
 /api/eval ────────▶│ Eval suite (UAT incidents become eval cases)  │
 /api/kb/sync ─────▶│ Celery KB-sync (git diff → incremental index) │
 /api/index ───────▶│ Indexing pipeline (tree-sitter → Neo4j/Weaviate)
 /api/graph ───────▶│ Graph read API                                │
 /api/feedback ────▶│ Feedback + reinforcement                      │
                    └───────┬────────────────────────┬─────────────┘
                            │                        │
             ┌──────────────▼───────┐      ┌─────────▼──────────────┐
             │  Stateless MCP :8002 │      │  celery-worker         │
             │  (16 read-only tools)│      │  (kb_sync, notifs)     │
             └──────────────────────┘      └────────────────────────┘
```

## What this branch contains beyond `main`

| Feature | What it does | Location |
|---------|--------------|----------|
| **Incident lifecycle** | Create incidents from panic logs, track state transitions (NEW → ANALYZED → PATCHED → RESOLVED) with timeline events | `backend/app/api/routers/incidents.py`, `core/incident_service.py`, `core/incident_store.py`, `core/state_machine.py`, `core/timeline_service.py` |
| **LLM agent** | LiteLLM-hosted model client; orchestrator runs the incident workflow (RAG context → model → root cause + patch) | `backend/app/agents/client.py`, `agents/orchestrator.py`, `agents/prompts/` |
| **RAG chat** | `/api/model/chat` — assembles semantic matches, file contents, and graph neighborhoods into a 120K-char context budget; answer streams back with citations; threads persisted in Postgres | `backend/app/api/routers/chat.py`, `rag/assembler/context_assembler.py`, `core/repositories/chat_repository.py` |
| **Resolution memory** | Resolved incidents stored as `ResolutionPackage` vectors in Weaviate; future similar panics semantically match past fixes | `core/resolved_error_store.py`, `core/learning_service.py`, `repositories/*resolved_error*` |
| **Eval suite** | UAT-resolved incidents automatically become eval cases; `/api/eval/run` replays them against the current model and scores pass/fail | `backend/app/api/routers/eval.py`, `core/eval_service.py`, `core/eval_store.py` |
| **Celery workers** | Async KB-sync (git diff → incremental reindex via queue) and notifications; runs as a separate 6GB `celery-worker` container | `backend/app/tasks/celery_app.py`, `tasks/workers/kb_sync.py`, `tasks/workers/notifications.py`, `docker-compose.yml` |
| **WebSockets** | Connection manager + incident room socket for the (deleted) frontend dashboard | `backend/app/api/websockets/` |
| **Postgres stores** | Incident, eval-case, resolved-error, and chat repositories (in-memory + Postgres implementations) | `backend/app/core/repositories/` |

## Additional environment variables (beyond `main`)

```bash
# LLM provider — required for agent, chat, and eval features
LITELLM_API_KEY=sk-...
LITELLM_BASE_URL=https://your-litellm-endpoint/v1
LITELLM_MODEL=glm-latest

# Store backends
INCIDENT_STORE_BACKEND=postgres
RESOLVED_ERROR_BACKEND=postgres
EVAL_CASE_BACKEND=postgres

# Celery
CELERY_TASK_ALWAYS_EAGER=false
```

## Reviving a feature on `main`

Each feature above is self-contained behind its router. To restore one:

1. Copy the module(s) from this branch into `main`
2. Re-register the router in `backend/app/main.py`
3. Re-add its env vars and, if needed, the `celery-worker` service in `docker-compose.yml`
4. Restore its tests from `backend/tests/`

The core RAG/indexing layer is identical on both branches, so the incident agent works against the same Neo4j/Weaviate/Postgres data.
