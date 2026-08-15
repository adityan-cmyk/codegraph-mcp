# TODO

## Completed

**WebSocket Live Updates:**
- Real WebSocket hook (`useWebSocket`) enhanced with `lastMessage` state and JSON error handling
- Hook wired into Dashboard — connects to `/ws/incident/{session_id}` when a session exists
- Handles `session_snapshot`, `sync_complete`, and `error` message types
- Live state badge showing current incident state in the AI Analysis header
- Connection status indicator in the right sidebar (WiFi icon + status text)

**Tailwind CSS + Component Fixes:**
- Fixed all 11 components that used undefined CSS class names (`badge-*`, `dashboard-card`, `section-title`, etc.)
- Converted all components to proper Tailwind utility classes
- `Badge` — fully working with 4 tones (neutral/success/warning/danger)
- `Spinner` — animated bouncing dots instead of plain text
- `IncidentChat` — renders timeline with proper styling
- `LlmStream` — displays analysis output, confidence scores, indexed symbols, graph reach
- `ResolveModal` — uses shadcn Button component
- `LangfuseTrace` — displays event stream with tracing status badge
- `McpTerminal` — sandbox status badges + cargo test/blame buttons
- `EnvSelector` — uses shadcn Button + Input components
- `LogInputArea` — uses shadcn Textarea
- `SyncKbButton` — uses shadcn Button
- `ThreePaneLayout` — uses Tailwind flex layout

**Component Wiring:**
- `IncidentChat` integrated into AI Analysis header (event timeline)
- `LlmStream` integrated into AI Analysis panel (inference output, confidence, symbols, graph reach)
- `McpTerminal` replaces inline sandbox terminal in Dashboard
- `EnvSelector` replaces inline environment/build selector
- `LogInputArea` replaces inline stack trace textarea
- `SyncKbButton` replaces inline KB sync button
- `ResolveModal` replaces inline resolve button
- `LangfuseTrace` remains in right sidebar

**Frontend Tests:**
- Vitest + @testing-library/react + jsdom configured
- 25+ tests across 7 test files, all passing
- `useWebSocket.test.ts` — connection, message handling, errors, malformed JSON
- `Badge.test.tsx` — label rendering, tone variants
- `LangfuseTrace.test.tsx` — empty state, timeline rendering
- `IncidentChat.test.tsx` — empty state, session info, timeline events
- `Dashboard.test.tsx` — renders all major sections including new components
- `Dashboard.websocket.test.tsx` — WebSocket-driven live updates (session_snapshot, sync_complete, error)
- `Dashboard.contextmenu.test.tsx` — chat history context menu (rename, delete, new chat)

**Langfuse Tracing Integration:**
- `langfuse>=2.0.0` added to `requirements.txt`
- `TracingService` with graceful no-op fallback when disabled
- Config settings: `LANGFUSE_ENABLED`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`
- Traced incident workflow: `context_assembly` span + `llm_resolution` generation
- Traced model chat endpoint
- `/api/tracing/status` endpoint for frontend to check if tracing is active
- Frontend shows tracing status badge in Trace Stream panel

**Health Check & Metrics Endpoints:**
- `GET /health` — basic liveness check (preserved)
- `GET /api/health` — detailed backend connectivity (Postgres, Redis, Weaviate, Neo4j)
- Each backend checked with appropriate health query; reports healthy/unhealthy/disabled
- `GET /api/metrics` — Prometheus-style metrics snapshot
  - Incident throughput (created/resolved/failed + per-minute rate)
  - Analysis latency (avg, p50, p95, p99)
  - Cache hit rate
  - Chat request count
  - KB sync total/failed
  - MCP tool calls/errors
  - Uptime

**Keyboard Shortcuts:**
- `Ctrl+Enter` — Start Analysis
- `Ctrl+K` — Focus chat input
- `Ctrl+N` — New incident
- `Ctrl+Shift+R` — Mark resolved
- `Ctrl+/` — Toggle shortcuts panel
- `Esc` — Close any dialog

**Index & Graph Explorer:**
- New "Index & Graph Explorer" section in center console
- Semantic query input + search button (queries `/api/index/query`)
- Graph traversal input + traverse button (queries `/api/index/graph/{symbol_id}`)
- Index stats cards (semantic docs, graph nodes)
- Results rendered as interactive lists

**Docker & Deployment:**
- `backend/Dockerfile` — multi-stage with health check, gcc/libpq for psycopg
- `frontend/Dockerfile` — multi-stage build (Node build → nginx serve) with API/WS proxy
- `docker-compose.yml` — full production stack:
  - Backend + Celery worker services (with all backend env vars)
  - Frontend service (nginx with proxy)
  - Postgres, Redis, Neo4j, Weaviate, t2v-transformers
  - Health checks on all services
  - Persistent volumes for all data stores
- `docker-compose.db.yml` — infra-only compose (preserved for local dev)
- `.github/workflows/ci.yml` — GitHub Actions CI:
  - Backend unit + integration tests (with Postgres + Redis services)
  - Frontend tests + build
  - Docker image builds on main push

**Validation Scripts:**
- `scripts/validate-infra.sh` — full endpoint + backend connectivity validation
- `scripts/validate-db-persistence.sh` — create incident, verify persistence after restart
- `scripts/validate-weaviate-neo4j.sh` — semantic query + graph traversal validation
- `scripts/validate-celery-redis.sh` — KB sync task queue validation

**Backend Test Coverage:**
- `tests/unit/test_incidents_api.py` — 15+ endpoint tests
- `tests/unit/test_rag_and_mcp.py` — 20+ RAG/MCP/KB tests
- `tests/unit/test_health_and_metrics.py` — health check + metrics collector tests
- `tests/integration/test_incident_lifecycle.py` — Postgres lifecycle + metrics endpoint tests
- `tests/integration/test_tracing.py` — TracingService unit + Langfuse integration tests

**Firecracker Sandbox:**
- `firecracker_mgr.py` upgraded from stub to pool manager with warm pool
- Pool creates up to 3 sandbox instances (Firecracker or Stub depending on availability)
- `acquire_microvm()` returns sandbox instance for execution
- `release_microvm()` returns sandbox to pool
- `pool_status()` endpoint for monitoring

**Metrics Collection:**
- `MetricsCollector` singleton tracks all key metrics with thread-safe locking
- Integrated into `IncidentService` (incident create/resolve/fail, analysis latency)
- Latency sliding window (keeps last 500 samples when over 1000)
- `/api/metrics` endpoint returns structured snapshot

## Remaining (Server Deployment)

These require the actual server environment to validate:

1. **DB persistence validation** — run `scripts/validate-db-persistence.sh` on server after restart
2. **Weaviate + Neo4j real flows** — run `scripts/validate-weaviate-neo4j.sh` with production backends
3. **Redis + Celery worker** — run `scripts/validate-celery-redis.sh` with real worker process
4. **Firecracker sandbox** — deploy kernel/rootfs to server, verify microVM execution
5. **End-to-end smoke test** — full user flow through frontend with real model + backends
