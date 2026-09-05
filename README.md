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

---

## Detailed documentation

---

## Architecture

```
┌──────────────────┐     ┌──────────────────┐     ┌────────────────────┐
│  opencode Agent  │────▶│  Stateless MCP   │────▶│  Neo4j             │  dependency graph (gen-tagged, usage-mode edges)
│  (any machine)   │     │  Server :8002    │────▶│  Weaviate          │  semantic vector search (shadow collections)
│                  │     │  (16 tools)      │────▶│  PostgreSQL        │  snapshots, feedback, build registry
│                  │────▶│  Feedback API    │     │  t2v-transformers  │  embedding model (BAAI/bge-base-en-v1.5)
└──────────────────┘     │  :8000           │     └────────────────────┘
                         └──────────────────┘
                                 │
                         ┌──────────────────┐
                         │  Reinforcement   │
                         │  Agent (daemon)  │
                         │  - quality gate  │
                         │  - build monitor │
                         │  - auto-rollback │
                         └──────────────────┘
```

### Services

| Service | Purpose | Port |
|---|---|---|
| `backend` | FastAPI + Starlette MCP server + static frontend | 8000, 8002 |
| `postgres` | Snapshots, AI feedback, build registry, event sourcing | 5433 |
| `redis` | Celery broker | 6380 |
| `neo4j` | Code dependency graph (generation-tagged, zero-downtime rebuild) | 7474/7687 |
| `weaviate` | Vector search (shadow collections, zero-downtime rebuild) | 8080 |
| `t2v-transformers` | sentence-transformers BAAI/bge-base-en-v1.5 | 8081 |

---

## Quick Start (Docker Compose)

### 1. Configure `.env`

```bash
LITELLM_BASE_URL=https://your-litellm-endpoint/v1
LITELLM_API_KEY=sk-your-key
LITELLM_MODEL=glm-latest
CODEBASE_ROOT_PATH=/repos/codebase
INDEX_REPLAY_ON_STARTUP=true
SEMANTIC_INDEX_BACKEND=weaviate
GRAPH_INDEX_BACKEND=neo4j
INDEX_METADATA_BACKEND=postgres
POSTGRES_DSN=postgresql://oncall:oncall@localhost:5433/oncall
WEAVIATE_URL=http://localhost:8080
NEO4J_URI=bolt://localhost:7687
```

### 2. Start DB services

```bash
docker compose -f docker-compose.db.yml up -d
```

### 3. Start backend

```bash
docker compose up -d backend
```

### 4. Access

- UI: `http://localhost:8000`
- MCP endpoint: `http://<host-ip>:8002/mcp`
- MCP tool directory: `http://<host-ip>:8002/` (GET)

SSH port forward:

```bash
ssh -L 8000:localhost:8000 -L 8002:localhost:8002 your-server
```

---

## MCP Server (Stateless, Read-Only)

The MCP server at `http://<host-ip>:8002/mcp` is **stateless** — no session IDs, no FastMCP. It implements the MCP JSON-RPC protocol over HTTP using a custom Starlette ASGI app. Each request is self-contained, so server restarts don't break connected clients.

### Connecting from opencode

Add this to your `opencode.json`:

```json
{
  "mcp": {
    "oncall-graph": {
      "url": "http://<host-ip>:8002/mcp"
    }
  }
}
```

### Available Tools (16)

| Tool | Description |
|---|---|
| `search_symbols` | Search symbols by partial name. Use this FIRST to find exact symbol_ids. |
| `search_symbols_enhanced` | Enhanced search with file path matching + fuzzy name resolution. Use when search_symbols returns 0. |
| `get_blast_radius` | Get immediate callers, callees, type-users, and used-types for a symbol. Includes risk score, risk factors, usage modes per edge, and optional `usage_modes_filter` to return only specific edge types (e.g. pattern_match, construction). |
| `get_blast_radius_detailed` | Blast radius with confidence scores AND usage modes on each edge (high/medium/low confidence). |
| `batch_blast_radius` | Get blast radius for multiple symbols at once. Returns combined impact analysis. |
| `get_symbol_content` | Get the full source code of a symbol. |
| `semantic_search` | Search by meaning (natural language). Hybrid BM25+vector, 30s timeout, auto-fallback to graph search. Chunk enrichment prepends module path for better matching. |
| `analyze_pr_diff` | Parse a git diff, extract changed symbols, classify change types (new, signature_change, body_only, trait_impl, struct_field, deleted), get blast radius for each — all in one call. |
| `find_dependency_path` | Find the shortest dependency path between two symbols. Prefers CALLS over USES, filters config types (GlobalState, etc.). |
| `traverse_graph` | Multi-hop dependency traversal from a symbol (depth 1-5). Supports `summary_only` mode for compact output (counts per hop only). |
| `get_graph_stats` | Get total graph nodes and edges. |
| `get_index_meta` | Get graph build metadata — gen number, commit hash, classifier version, timestamp, embedding model, features list. Use to verify graph freshness. |
| `get_symbols_in_file` | List all symbols defined in a file. Use to resolve a diff's file path to exact symbols without guessing. |
| `submit_search_feedback` | Rate semantic_search results (+1 helpful, -1 not helpful, or query-level with no symbol). |
| `submit_ai_feedback` | Submit full post-analysis feedback summary after PR review. PRIMARY feedback mechanism. |
| `get_reinforcement_stats` | Get reinforcement learning statistics — boosted/penalized symbols, query expansions. |

### Tool Usage Guide

#### `search_symbols`
Search the dependency graph by partial symbol name. Returns matching symbol IDs.
```json
{"query": "InventoryClient", "limit": 20}
```
- Use this **first** when you don't know the exact `symbol_id`.
- Returns: `symbol_id`, `short_name`, flags for has_calls/has_uses/has_callers/has_users.

#### `search_symbols_enhanced`
Fallback search when `search_symbols` returns 0 matches. Searches file paths, short names, and uses fuzzy matching.
```json
{"query": "deactivate_customer", "search_files": true, "fuzzy": true}
```
- Searches both symbol names and file paths (e.g. `dormancy_report.rs`).
- Fuzzy matching catches typos and partial names.
- Returns: `symbol_id`, `short_name`, `file_path`, `match_type` (exact/fuzzy/file_path).

#### `get_blast_radius`
Get the immediate impact radius of a symbol — who calls it, what it calls, what types use it.
```json
{"symbol_id": "crates::inventory::client::InventoryClient"}
```
- Returns: `upstream` (callers), `downstream` (callees), `used_by` (type users), `uses` (type refs).
- Includes `risk_score` (low/medium/high/critical) and `risk_factors` with formula, thresholds, and human-readable reasons.
- Includes `used_by_modes` and `uses_modes` — per-edge usage mode classification (pattern_match, construction, field_declaration, trait_impl, type_param, import, type_reference).
- Includes `usage_mode_summary` — aggregate counts per mode.
- Optional `usage_modes_filter` to return only matching edges:
```json
{"symbol_id": "crates::common::redis::connection::ConcreteConnection", "usage_modes_filter": ["pattern_match", "construction"]}
```
- Includes `kind`, `file_path`, `start_line`, `end_line`.

#### `get_blast_radius_detailed`
Same as `get_blast_radius` but with **confidence scores** AND **usage modes** on each edge.
```json
{"symbol_id": "crates::inventory::client::InventoryClient"}
```
- Each edge has a `confidence` field: `high` (direct call), `medium` (type reference), `low` (inferred).
- Each `used_by` and `uses` edge includes `usage_modes` — how the caller references the target (pattern_match, construction, field_declaration, trait_impl, type_param, import, type_reference).
- Use this when you need to know how each caller uses the symbol and how reliable each connection is.

#### `batch_blast_radius`
Get blast radius for multiple symbols in a single call. Returns combined impact analysis.
```json
{"symbol_ids": ["crates::wallet::transfer", "crates::wallet::validate"]}
```
- Returns per-symbol blast radius + `combined_impact` with unique caller/callee counts.
- Use this when a PR changes several symbols.

#### `get_symbol_content`
Get the full source code of a symbol.
```json
{"symbol_id": "crates::inventory::client::InventoryClient"}
```
- Returns: `content` (source code, max 8000 chars), `file_path`, `start_line`, `end_line`, `truncated`.
- Use after `get_blast_radius` to understand what a symbol actually does.

#### `semantic_search`
Search the codebase by meaning, not by name. Hybrid BM25+vector search with 30s timeout and auto-fallback to graph search.
```json
{"query": "payment validation logic", "limit": 10}
```
- Hybrid search: BM25 keyword matching + vector similarity (alpha=0.5).
- Chunk enrichment: module path prepended to chunk text before embedding for better semantic matching.
- Returns: `symbol_id`, `score`, `file_path`, `content_preview` for each match.
- Includes `boost` from reinforcement learning (feedback-adjusted ranking).
- If Weaviate is slow, falls back to graph-based `search_symbols`.
- After reviewing results, call `submit_search_feedback` to rate them.

#### `analyze_pr_diff`
Parse a git diff, extract changed symbols, classify change types, resolve them against the graph, and get blast radius — all in one call.
```json
{"diff_text": "diff --git a/src/payment.rs\n...", "max_symbols": 10}
```
- Extracts function/struct/trait/enum names from the diff.
- Classifies change types: `new`, `signature_change`, `body_only`, `trait_impl`, `struct_field`, `deleted`.
- Resolves each name to a `symbol_id` in the graph.
- Returns blast radius with risk factors for each resolved symbol + combined impact.
- Includes `change_type_breakdown` summary.
- Replaces the manual `search_symbols` + `get_blast_radius` workflow.

#### `find_dependency_path`
Find the shortest dependency path between two symbols.
```json
{"from_symbol": "crates::wallet::transfer", "to_symbol": "crates::inventory::client::InventoryClient", "max_depth": 5}
```
- Returns: `path` (array of symbol IDs), `readable_path` (A -> B -> C), `path_length`.
- Prefers CALLS-only paths first, falls back to CALLS|USES with config types (GlobalState, etc.) filtered out.
- Use this to understand how a change to one symbol could affect another.

#### `traverse_graph`
Multi-hop dependency traversal from a symbol up to N hops.
```json
{"symbol_id": "crates::inventory::client::InventoryClient", "depth": 2}
```
- Returns neighborhoods for every reachable symbol (max 50).
- Use this when a change is significant (signature change, removed function, new trait impl).
- `depth=1` = same as `get_blast_radius`, `depth=2` = immediate + transitive.
- Supports `summary_only=true` for compact output (counts per hop only, no full neighborhood data):
```json
{"symbol_id": "crates::inventory::client::InventoryClient", "depth": 2, "summary_only": true}
```

#### `get_graph_stats`
Get current graph index statistics.
```json
{}
```
- Returns: `graph_nodes`, `graph_edges`.
- Safe to call anytime. No arguments needed.

#### `submit_search_feedback`
Rate `semantic_search` results to improve future search quality.
```json
{"query_text": "payment validation", "symbol_id": "crates::payment::validate", "feedback": 1, "reason": "Exactly what I needed"}
```
- `feedback`: `1` (helpful), `-1` (not helpful), `0` (query-level only).
- For query-level feedback (entire result set was poor), omit `symbol_id`:
```json
{"query_text": "dormancy report", "reason": "Expected to find dormancy_report.rs but got unrelated results"}
```
- Feedback feeds into the reinforcement learning pipeline.

#### `submit_ai_feedback`
Submit a full post-analysis feedback summary after completing a PR review. **PRIMARY feedback mechanism.**
```json
{
  "client_id": "opencode-agent",
  "pr_context": "PR #123: Add inventory tagging",
  "tools_called": [
    {"tool": "semantic_search", "args": {"query": "inventory"}, "result_summary": "Found 5 results"}
  ],
  "results_used": [
    {"symbol_id": "crates::inventory::client::InventoryClient", "file_path": "crates/inventory/client.rs", "helpful": true}
  ],
  "results_expected": "Expected to find tag_inventory function",
  "quality_rating": 3,
  "improvement_suggestions": "Search could match on partial function names"
}
```
- Goes through a quality gate (only specific, actionable feedback is accepted).
- At 10 accepted feedbacks, a new build is auto-triggered with reranked search.

#### `get_reinforcement_stats`
Get reinforcement learning statistics.
```json
{}
```
- Returns: `total_feedback`, `boosted_symbols`, `penalized_symbols`, `query_expansions`, `top_adjusted_symbols`.
- Shows which symbols are boosted/penalized from accumulated feedback.

#### `get_index_meta`
Get graph build metadata for trust verification.
```json
{}
```
- Returns: `graph_gen`, `classifier_version`, `last_indexed_commit`, `current_head`, `up_to_date`, `snapshot_created_at`, `files_indexed`, `total_symbols`, `total_edges`, `weaviate_collection`, `embedding_model`, `embedding_dimensions`, `features`.
- Use this to verify the graph is fresh and which classifier version was used before relying on usage modes or risk factors.

#### `get_symbols_in_file`
List all symbols defined in a file.
```json
{"file_path": "crates/common/src/redis/wrapper.rs"}
```
- Returns: `file_path`, `symbols_found`, `symbols` (array of `{symbol_id, kind, start_line, end_line}`).
- Use this to resolve a diff's file path to exact symbols — avoids guessing symbol names.

### Usage from any client

```bash
# List all tools
curl -s http://<host-ip>:8002/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'

# Call a tool
curl -s http://<host-ip>:8002/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_graph_stats","arguments":{}},"id":2}'
```

### Tool Directory (GET /)

```bash
curl -s http://<host-ip>:8002/ | python3 -m json.tool
```

Returns a full directory of all tools, their arguments, and response shapes.

---

## PR Review Workflow

When reviewing a PR, use the `oncall-graph` MCP tools to understand blast radius and dependencies:

1. **Verify graph freshness**: Call `get_index_meta` to confirm the graph is up-to-date with the latest commit.
2. **Resolve changed symbols**: Call `analyze_pr_diff` with the full diff text — it classifies change types (new, signature_change, body_only, trait_impl, struct_field, deleted) and gets blast radius for each.
3. **Filter by impact**: Use `get_blast_radius` with `usage_modes_filter=["pattern_match", "construction"]` to find only callers that would break on shape changes.
4. **Deep traversal**: If the change is significant (signature change, removed function, new trait impl), call `traverse_graph` with `depth=2` and `summary_only=true` for a compact blast radius size estimate.
5. **File-level lookup**: Use `get_symbols_in_file` to resolve a diff's file path to exact symbols when symbol names are ambiguous.
6. **Risk assessment**: Flag any PR that modifies a symbol with `risk_score: critical` or `high`, or with >10 downstream callers or >5 upstream callers.
7. **Feedback**: After review, call `submit_search_feedback` to rate result quality, and call `submit_ai_feedback` with a post-analysis summary.

---

## Reinforcement Learning System

The system improves search quality over time through a multi-stage feedback pipeline. Each time an external opencode agent uses the MCP tools for a PR review, it submits feedback about what was helpful, what was missing, and what could be better. The system learns from this feedback and applies it to future searches.

### Decision Pipeline

```
Model A says: "InventoryClient was helpful, tag_inventory was not"
Model B says: "search for 'payment' returned garbage, expected PaymentValidator"
Model C says: "blast_radius was great, semantic_search missed the trait impl"
                    │
                    ▼
        ┌─── Quality Gate (per feedback) ───┐
        │ Did it call tools?                 │
        │ Did it cite specific symbols?      │  → reject vague ones
        │ Did it say what was expected?      │
        │ Did it give actionable suggestions?│
        └────────────────────────────────────┘
                    │ accepted only
                    ▼
        ┌─── Signal Extraction ─────────────┐
        │ Per-symbol: helpful=true → +weight │
        │ Per-symbol: helpful=false → -weight│
        │ results_expected → gap detected    │
        │ rating ≤2 + specifics → strong -   │
        │ rating ≥4 + specifics → strong +   │
        └────────────────────────────────────┘
                    │
                    ▼
        ┌─── Aggregation ───────────────────┐
        │ Multiple models voting on same    │
        │ symbol → weighted average         │
        │ 33 models say InventoryClient +   │
        │ 0 say - → boost_weight = +0.74    │
        │ 31 say tag_inventory -            │
        │ 0 say + → boost_weight = -1.0     │
        └────────────────────────────────────┘
                    │
                    ▼
        ┌─── Live Application ──────────────┐
        │ semantic_search reranks results:  │
        │ final_score = embedding_score     │
        │             + 0.15 * boost_weight │
        │ Query expansion adds terms from   │
        │ positive feedback                 │
        └────────────────────────────────────┘
                    │
                    ▼
        ┌─── At 10 accepted → Auto-build ───┐
        │ New Weaviate collection (shadow)  │
        │ Old keeps serving queries         │
        │ Feedback marked consumed          │
        │ Build quality tracked vs parent   │
        │ If worse → auto-rollback          │
        └────────────────────────────────────┘
```

**Key point**: no single model run can hijack the system. A symbol needs **multiple models** voting the same way before the weight moves significantly. The formula is `boost_weight = (positive - negative) / (positive + negative + 5) * 2 - 1`, so 1 vote barely moves it, but 30+ votes converge to ±1.0. This means the system gets smarter with each PR review, but no single agent can game the rankings.

### Flow

```
External AI Agent ──submit_ai_feedback (MCP)──▶ [pending feedback in Postgres]
                  ──POST /api/feedback/ai────▶ [pending feedback in Postgres]
                                                       │
                           Reinforcement Agent (every 5 min)
                           ├── Quality Gate: accepts/rejects (score > 0.5)
                           ├── Extracts per-symbol boost/penalty signals
                           ├── Applies reranking weights to semantic_search
                           ├── Auto-triggers rebuild at 10 accepted feedbacks
                           ├── Monitors build quality vs parent
                           └── Triggers rollback if quality regresses
```

### Quality Gating

Not all feedback is consumed. Each entry is evaluated on:

- **Tool usage** — did the agent actually call MCP tools?
- **Specificity** — does it reference specific symbol_ids or file paths?
- **Constructiveness** — does it provide actionable improvement suggestions?
- **Expected results** — does it describe what was missing?
- **Rating consistency** — does the rating match the feedback content?
- **Length** — very short feedback (<50 chars) is rejected

Score > 0.5 = accepted, below = rejected. Vague or generic feedback like "bad" or "good" is automatically rejected.

### Auto-Build Trigger

When 10 or more accepted (but unconsumed) feedback entries accumulate, the reinforcement agent automatically triggers a zero-downtime rebuild:

1. New Weaviate shadow collection created — old one keeps serving queries
2. Semantic index rebuilt from the Postgres snapshot
3. Atomic swap — new collection becomes active
4. Consumed feedback marked with the new `build_id`
5. Build quality score tracked against parent build
6. If quality drops 15%+ below parent → automatic rollback

### Build Versioning & Rollback

Every graph+semantic rebuild is registered in the build registry:

- Each build has a `build_id` (UUID) and `parent_build_id` (forming a chain)
- Builds track: Weaviate collection name, Neo4j generation, quality score
- When a new build's quality score drops 15%+ below the parent, **automatic rollback**:
  - Weaviate: swaps active collection back to the previous one
  - Neo4j: swaps active generation back to the previous one
  - No re-indexing needed — old data is kept for rollback
- Manual rollback: `POST /api/feedback/build/rollback`

### Zero-Downtime Rebuilds

Both Weaviate and Neo4j support zero-downtime rebuilds:

- **Weaviate**: new data goes into a shadow collection while the old one keeps serving queries. Atomic swap on commit. Old collection kept for rollback.
- **Neo4j**: new data uses a new generation tag (`gen=N+1`). Atomic swap via `GraphIndexProxy`. Old generation kept for rollback.
- Tested: 206 concurrent queries during rebuild, 0 failures.

### Feedback API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/feedback/ai` | POST | Submit post-analysis feedback from an AI agent |
| `/api/feedback/ai/stats` | GET | Feedback counts by status, average scores |
| `/api/feedback/simple` | POST | Submit simple per-symbol feedback |
| `/api/feedback/evaluate` | POST | Run quality gating on pending feedback |
| `/api/feedback/reinforcement/stats` | GET | Boost weights, query expansions |
| `/api/feedback/build/stats` | GET | Build registry stats |
| `/api/feedback/build/history` | GET | Build history with quality scores |
| `/api/feedback/build/rollback` | POST | Rollback to previous build |

### AI Feedback Schema

```json
POST /api/feedback/ai
{
  "client_id": "opencode-agent-1",
  "pr_context": "PR #123: Add inventory tagging",
  "tools_called": [
    {"tool": "semantic_search", "args": {"query": "inventory"}, "result_summary": "Found 5 results"}
  ],
  "results_used": [
    {"symbol_id": "crates::inventory::client::InventoryClient", "file_path": "...", "helpful": true}
  ],
  "results_expected": "Expected to find tag_inventory function",
  "quality_rating": 3,
  "improvement_suggestions": "Search could match on partial function names"
}
```

---

## Codebase Indexing

The system indexes a Rust repository into two stores:

- **Weaviate** — semantic vector search (BAAI/bge-base-en-v1.5, 768-dim, batched 64 chunks at a time, hybrid BM25+vector search)
- **Neo4j** — code dependency graph (calls + uses relationships, generation-tagged, usage-mode annotated edges)
- **PostgreSQL** — source of truth (snapshots: chunks + graph edges with usage modes)

### Indexing pipeline

1. Regex parser extracts `.rs` file symbols → `CodeChunk` objects (functions, structs, impls, traits, enums)
2. Call targets extracted via `CALL_PATTERN`, `METHOD_CALL_PATTERN`
3. Type references extracted via `TYPE_REF_PATTERN` (matches `impl X`, `: Type`, `-> Type`, `<Type>`, and `TypeName::Variant`)
4. Usage modes classified at index time: `pattern_match`, `construction`, `field_declaration`, `trait_impl`, `type_param`, `import`, `type_reference`
5. Impl block methods extracted as separate symbols
6. Graph edges built from call/type relationships with usage modes stored on USES edges
7. Chunk enrichment: module path prepended to content before embedding
8. Snapshot stored in Postgres (incremental updates, not full re-insert)
9. Graph built in Neo4j (zero-downtime, gen-tagged, usage modes on edges)
10. Semantic index built in Weaviate (zero-downtime, shadow collection)

### Incremental Ingest

Nightly cron job (`scripts/nightly-sync.sh`) runs at midnight:
1. `git pull --ff-only origin master` on the host
2. `POST /api/index/ingest` — git diff between last indexed commit and HEAD
3. Blast-radius expansion: changed files + their dependents (callers/callees) re-indexed
4. Only changed chunks re-embedded (batched 64 at a time)
5. Incremental DB update (only touched rows, not full snapshot re-insert)

### Monitor indexing

```bash
curl -s http://localhost:8000/api/index/stats | python3 -m json.tool
```

### Manual re-index

```bash
curl -X POST http://localhost:8000/api/index/repository \
  -H "Content-Type: application/json" \
  -d '{"repository_path": "/repos/codebase"}'
```

### Rebuild semantic index only (no re-parsing)

```bash
curl -X POST http://localhost:8000/api/index/semantic/rebuild
```

---

## API Endpoints

### Indexing
- `POST /api/index/repository` — Index Rust repository
- `GET /api/index/stats` — Get index statistics
- `POST /api/index/query` — Semantic search query (hybrid BM25+vector)
- `GET /api/index/graph/{symbol_id}` — Graph neighborhood
- `POST /api/index/replay` — Replay indexes from storage
- `POST /api/index/semantic/rebuild` — Rebuild semantic index only
- `POST /api/index/ingest` — Incremental git-diff ingest (changed files + dependents)
- `GET /api/index/ingest/status` — Check last indexed commit vs HEAD

### Graph (read-only REST)
- `GET /api/graph/blast-radius/{symbol_id}` — Blast radius query
- `GET /api/graph/traverse/{symbol_id}?depth=2` — Graph traversal
- `GET /api/graph/stats` — Graph stats
- `GET /api/graph/has/{symbol_id}` — Check if symbol exists

### Feedback & Reinforcement
- `POST /api/feedback/ai` — Submit AI agent feedback
- `POST /api/feedback/simple` — Submit simple per-symbol feedback
- `POST /api/feedback/evaluate` — Run quality gating
- `GET /api/feedback/ai/stats` — Feedback statistics
- `GET /api/feedback/reinforcement/stats` — Reinforcement learning stats
- `GET /api/feedback/build/stats` — Build registry stats
- `GET /api/feedback/build/history` — Build history
- `POST /api/feedback/build/rollback` — Rollback to previous build

### Chat
- `POST /api/model/chat` — RAG-enabled chat with codebase context

### Incidents
- `POST /api/incidents/` — Create incident session
- `GET /api/incidents/` — List all incidents
- `GET /api/incidents/{session_id}` — Get incident details
- `POST /api/incidents/{session_id}/state` — Transition state
- `POST /api/incidents/{session_id}/analyze` — Run analysis
- `WS /ws/incident/{session_id}` — WebSocket incident room

### MCP (stateless, port 8002)
- `POST /mcp` — MCP JSON-RPC (initialize, tools/list, tools/call, ping)
- `GET /` — Tool directory
- `GET /health` — Health check

---

## Data Safety & Recovery

The system separates **source of truth** (postgres) from **derived indexes** (weaviate, neo4j):

- **Postgres snapshots are atomic** — `replace_snapshot` runs in a single transaction. If the process crashes mid-write, the transaction rolls back and the previous snapshot remains intact.
- **Weaviate data is ephemeral** — rebuildable from the postgres snapshot. Zero-downtime rebuild uses shadow collections.
- **Neo4j graph** rebuilds from postgres snapshot. Zero-downtime rebuild uses generation tagging.
- **Build registry** tracks all builds with parent chains for rollback.
- **Never wipe postgres** unless you want to re-parse the entire repository.

---

## Security

### Supply Chain

| Measure | Status |
|---|---|
| **pip hash-pinning** | `requirements.txt` generated with `pip-compile --generate-hashes`, installed with `--require-hashes` |
| **npm ci** | Frontend uses `npm ci --audit-level=high` in Dockerfile and CI |
| **Rustup checksum** | SHA256 checksum verification before executing rustup-init |
| **Model pinning** | Embedding model `BAAI/bge-base-en-v1.5` preloaded at build time |
| **SBOM** | Syft generates SPDX SBOM on every image build (CI) |
| **Image signing** | Cosign keyless signing of built images (CI) |
| **Dependency audit** | `pip-audit` + `npm audit` in CI security-scans job |
| **Secret scanning** | Gitleaks runs on every push/PR |
| **Container scanning** | Trivy scans both filesystem and built images |

### Container Hardening

| Measure | Implementation |
|---|---|
| Non-root user | `appuser` (UID auto-assigned, `/sbin/nologin` shell) |
| `no-new-privileges` | Prevents privilege escalation via setuid binaries |
| `read_only` rootfs | Container filesystem is read-only; `/tmp` and `/app/.cache` are tmpfs |
| `cap_drop: ALL` | All Linux capabilities dropped |
| Resource limits | CPU and memory limits on all services |
| Concurrency limits | `--limit-concurrency 100` + `--timeout-keep-alive 30` on uvicorn |

### API Security

| Measure | Implementation |
|---|---|
| Bearer token auth | `AuthMiddleware` on all `/api/` routes (configurable via `API_AUTH_TOKEN`) |
| Trusted host check | `TrustedHostMiddleware` rejects unknown host headers |
| Rate limiting | Per-IP, per-endpoint-class: index/admin=5/min, mutation=20/min, default=100/min |
| Request size limit | 10MB max request body (413 on exceed) |
| CORS | Configurable allowed origins (default: localhost only) |
| Read-only MCP | All 16 MCP tools are read-only — no mutation of indexes or data |

### Rate Limiting

Rate limits are enforced by `RateLimitMiddleware` with sliding 60-second windows:

| Route class | Limit | Examples |
|---|---|---|
| Index/admin | 5 req/min | `/api/index/repository`, `/api/index/semantic/rebuild`, `/api/index/replay` |
| Mutation | 20 req/min | `/api/incidents`, `/api/model/chat` |
| Default | 100 req/min | All other routes |

Responses include `X-RateLimit-Remaining` header. 429 responses include `Retry-After`.

---

## Infrastructure

### Structured Logging

All logs are emitted as JSON with trace IDs. The `TraceIdMiddleware` adds a `X-Trace-Id` header to every request and includes it in log output, enabling request tracing across services.

```json
{"timestamp": "2026-07-27T...", "level": "INFO", "trace_id": "abc-123", "message": "..."}
```

### Model Provider Abstraction

The `model_provider.py` module provides a unified interface for LLM calls (`ModelClient` protocol). The default implementation is `LiteLLMModelClient`, which wraps LiteLLM with configurable timeouts and retries. This allows swapping the model provider without touching business logic.

### Health & Readiness

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness probe — process is running |
| `GET /ready` | Readiness probe — all backends reachable, model available |
| `GET /api/health` | Detailed health: per-backend status (postgres, redis, weaviate, neo4j, model) |

### Outbound Timeouts & Retries

Configured in `config.py`:

| Setting | Default | Description |
|---|---|---|
| `OUTBOUND_TIMEOUT_SECONDS` | `30` | Timeout for outbound HTTP calls |
| `OUTBOUND_RETRY_COUNT` | `3` | Retry attempts for transient failures |
| `SEMANTIC_SEARCH_TIMEOUT_SECONDS` | `30` | Hard timeout for Weaviate queries (falls back to graph search) |

### Architecture Decision Records

ADRs are in `docs/adr/000-architecture-decisions.md`, covering:

- MCP stateless architecture (no sessions, custom Starlette ASGI)
- Zero-downtime rebuilds (Weaviate shadow collections, Neo4j gen tagging)
- Reranking refresh strategy (incremental at 5, full rebuild at 10)
- Quality gating for AI feedback (score > 0.5 threshold)
- Model provider abstraction
- Hash-pinned dependencies

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `INDEX_ON_STARTUP` | `false` | Auto-index on container start |
| `INDEX_REPLAY_ON_STARTUP` | `true` | Replay stored indexes on start |
| `CODEBASE_ROOT_PATH` | — | Path to Rust repo (inside container) |
| `SEMANTIC_INDEX_BACKEND` | `memory` | `memory` or `weaviate` |
| `GRAPH_INDEX_BACKEND` | `memory` | `memory` or `neo4j` |
| `INDEX_METADATA_BACKEND` | `memory` | `memory` or `postgres` |
| `POSTGRES_DSN` | `postgresql://localhost/oncall` | Postgres connection string |
| `WEAVIATE_URL` | `http://localhost:8080` | Weaviate URL |
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j bolt URI |
| `READONLY_MCP_HOST` | `0.0.0.0` | MCP server bind host |
| `READONLY_MCP_PORT` | `8002` | MCP server port |
| `LITELLM_BASE_URL` | — | LiteLLM endpoint URL |
| `LITELLM_API_KEY` | — | LiteLLM API key |
| `LITELLM_MODEL` | `glm-latest` | Model name |
| `API_AUTH_TOKEN` | — | Bearer token for API auth (empty = no auth) |
| `TRUSTED_HOSTS` | `["localhost","127.0.0.1","testserver"]` | Allowed host headers |
| `CORS_ALLOWED_ORIGINS` | `["http://localhost:8000"]` | CORS allowed origins |
| `OUTBOUND_TIMEOUT_SECONDS` | `30` | Timeout for outbound HTTP calls |
| `OUTBOUND_RETRY_COUNT` | `3` | Retry attempts for transient failures |
| `SEMANTIC_SEARCH_TIMEOUT_SECONDS` | `30` | Weaviate query timeout before fallback |

---

## License

MIT
