# codegraph-mcp

A stateless **MCP server** for code dependency graph analysis of Rust codebases — blast radius, semantic search, PR diff analysis, a metal detector for bugs hiding in comments and dead code (TODOs, swallowed errors, stubs), and a reinforcement loop that improves search quality from AI agent feedback. 17 of the 19 tools are read-only analysis (including a local decision-model tool); the two feedback tools write only to feedback tables.

> **Note:** the full on-call assistant (incident lifecycle, LLM agent, chat, eval suites) lives on the [`oncall-assistant`](../../tree/oncall-assistant) branch. This branch is the MCP server only.

---

## Architecture

```
┌──────────────────┐     ┌──────────────────┐     ┌────────────────────┐
│  opencode Agent  │────▶│  Stateless MCP   │────▶│  Neo4j             │  dependency graph (gen-tagged, usage-mode edges)
│  (any machine)   │     │  Server :8002    │────▶│  Weaviate          │  semantic vector search (shadow collections)
│                  │     │  (19 tools)      │────▶│  PostgreSQL        │  snapshots, feedback, build registry
│                  │────▶│  Feedback API    │     │  t2v-transformers  │  embedding model (BAAI/bge-base-en-v1.5)
└──────────────────┘     │  :8000           │     │  ollama            │  decision model (nimble 9B, feedback gate)
                         └──────────────────┘     └────────────────────┘
                                 │
                         ┌──────────────────┐
                         │  Reinforcement   │
                         │  Agent (daemon)  │
                         │  - quality gate  │
                         │  - build monitor │
                         │  - auto-rollback │
                         └──────────────────┘

Observability (internal-only except Grafana):

┌─────────────────┐  10s scrape (bearer)  ┌──────────────┐
│ backend         │──────────────────────▶│ Prometheus   │──┐
│ /api/metrics    │                       │ (30d TSDB)   │  │
└─────────────────┘                       └──────────────┤  │
┌─────────────────┐  10s cadvisor         ┌──────────────┐│  │
│ containers      │──────────────────────▶│              ││  │
└─────────────────┘                       │  Grafana     │◀─┘
┌─────────────────┐  10s docker_sd        │  :3000       │
│ container logs  │──────────────────────▶│ (dashboards) │
└─────────────────┘   ┌──────────────┐    │              │
                      │ Loki         │───▶│              │
                      │ (30d logs)   │    └──────────────┘
                      └──────────────┘
```

**Security posture of the observability stack**: Prometheus is pull-only (remote-write disabled, no host port), Loki's push API is reachable only inside the compose network (promtail is the sole writer), and Grafana is the only exposed service — admin login required, sign-up disabled, viewers are read-only. The scrape target `/api/metrics` sits behind the same `API_AUTH_TOKEN` as the rest of the API; Prometheus injects the token at startup from env.

An interactive version of this diagram with full explanations lives in [docs/architecture.html](docs/architecture.html).

### Services

| Service | Purpose | Port |
|---|---|---|
| `backend` | FastAPI API + stateless MCP server | 8000, 8002 |
| `postgres` | Snapshots, AI feedback, build registry | 5433 |
| `redis` | Rate limiting, build stats, known-client tracking | 6380 |
| `neo4j` | Code dependency graph (generation-tagged, zero-downtime rebuild) | 7474/7687 |
| `weaviate` | Vector search (shadow collections, zero-downtime rebuild) | 8080 |
| `t2v-transformers` | sentence-transformers BAAI/bge-base-en-v1.5 | 8081 |
| `ollama` | Decision model for feedback quality gate (nimble 9B) | — |
| `prometheus` | Metrics TSDB, 10s scrape, 30d retention | — (internal) |
| `loki` | Log aggregation, 30d retention | — (internal) |
| `promtail` | Ships oncall-* container logs to Loki | — |
| `cadvisor` | Per-container CPU/memory metrics | — (internal) |
| `grafana` | Dashboards (the only exposed observability port) | 3000 |

---

## Quick Start (Docker Compose)

### 1. Configure `.env`

```bash
CODEBASE_REPO_PATH=/path/to/your/rust/codebase
CODEBASE_GIT_BRANCH=master
CODEBASE_ROOT_PATH=/repos/codebase
INDEX_REPLAY_ON_STARTUP=true
SEMANTIC_INDEX_BACKEND=weaviate
GRAPH_INDEX_BACKEND=neo4j
INDEX_METADATA_BACKEND=postgres
POSTGRES_DSN=postgresql://oncall:oncall@localhost:5433/oncall
WEAVIATE_URL=http://localhost:8080
NEO4J_URI=bolt://localhost:7687
MCP_AUTH_TOKEN=your-secret-token
SMTP_HOST=smtp.gmail.com
SMTP_USER=you@gmail.com
SMTP_PASSWORD=your-app-password
SMTP_TO=["you@gmail.com"]
```

### 2. Start everything

```bash
docker compose up -d
```

### 3. Access

- API health: `http://localhost:8000/health`
- MCP endpoint: `http://<host-ip>:8002/mcp`
- MCP tool directory: `http://<host-ip>:8002/` (GET)

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

### Available Tools (24)

| Tool | Description |
|---|---|
| `search_symbols` | Search symbols by partial name. Use this FIRST to find exact symbol_ids. |
| `search_symbols_enhanced` | Enhanced search with file path matching + fuzzy name resolution + multi-word queries. Use when search_symbols returns 0. |
| `get_blast_radius` | Get immediate callers, callees, type-references, and used-types for a symbol. Direct callers (CALLS edges) and type references (USES edges) are reported separately, never conflated. Includes risk score, risk factors, usage modes per edge, `usage_modes_filter`, and `exclude_shared_state_types` (filters god-types like GlobalState from the type expansion). |
| `get_blast_radius_detailed` | Blast radius with confidence scores AND usage modes on each edge (high/medium/low confidence). |
| `batch_blast_radius` | Get blast radius for multiple symbols at once. Returns combined impact analysis. |
| `get_symbol_content` | Get the source code of a symbol. Supports `start_line`/`end_line` paging for 500+ line symbols. |
| `semantic_search` | Search by meaning (natural language). Hybrid BM25+vector, 30s timeout, auto-fallback to graph search. Chunk enrichment prepends module path for better matching. |
| `analyze_pr_diff` | Parse a git diff, extract changed symbols, classify change types (new, signature_change, body_only, trait_impl, struct_field, deleted), get blast radius for each — all in one call. Symbol resolution is scoped to the diff's files. |
| `find_dependency_path` | Find the shortest dependency path between two symbols. Prefers CALLS over USES, filters config types (GlobalState, etc.). |
| `traverse_graph` | Multi-hop dependency traversal from a symbol (depth 1-5). Supports `summary_only` mode for compact output (counts per hop only, up to 1000 neighborhoods). |
| `get_graph_stats` | Get total graph nodes and edges, with the node-vs-symbol-count reconciliation (graph excludes file summaries, module exports, trivial constants). |
| `get_index_meta` | Get graph build metadata — gen number, commit hash, commits_behind (drift), per-file coverage stats, classifier version, timestamp, embedding model. Use to verify graph freshness. |
| `get_symbols_in_file` | List all symbols defined in a file. Use to resolve a diff's file path to exact symbols without guessing. |
| `find_warnings_in_blast_radius` | **Metal detector**: TODO/FIXME/HACK comments, swallowed errors (`let _ = fn()`), logging-only stub functions, commented-out code, and auth fail-open defaults (`unwrap_or_default()`) within a symbol's dependency blast radius. The bug classes that live in comments and dead code — invisible to the dependency graph. |
| `resolve_stacktrace` | Resolve a raw Rust panic backtrace to graph symbols with source context. |
| `find_dead_code` | Find symbols with no callers, no type-references, and no uses — candidates for deletion. |
| `recent_changes_near` | Commits from the last N days touching a symbol directly or through its blast radius files. |
| `find_hotspots` | Rank files by git churn x graph connectivity x missing test coverage — where regressions are most likely. |
| `get_tests_for_symbol` | Find test fns that call a symbol or reference its type (`#[test]`, `#[tokio::test]`, `#[cfg(test)]` modules). Use before changes to gauge regression coverage; test callers are excluded from risk scores. |
| `diff_modules` | Symbol-set drift between counterpart modules (sync vs async report generators, handler mirrors). Catches implementations that have drifted apart. |
| `submit_search_feedback` | Rate semantic_search results (+1 helpful, -1 not helpful, or query-level with no symbol). |
| `submit_ai_feedback` | Submit full post-analysis feedback summary after PR review. PRIMARY feedback mechanism. |
| `get_reinforcement_stats` | Get reinforcement learning statistics — boosted/penalized symbols, query expansions. |
| `make_decision` | Typed judgments (noul yes/no, score, choice) from the local decision model. Single model pass for all questions (~10-60s); for decisions worth waiting on — PR risk, triage, gating. |

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
- Returns: `content` (source code, max 8000 chars), `file_path`, `start_line`, `end_line`, `total_lines`, `truncated`, and a paging hint.
- For 500+ line symbols the content is truncated — re-call with `start_line`/`end_line` (absolute file line numbers within the symbol's span) to page through the rest:
```json
{"symbol_id": "crates::inventory::client::InventoryClient", "start_line": 200, "end_line": 400}
```
- Use after `get_blast_radius` to understand what a symbol actually does.

#### `find_warnings_in_blast_radius`
The metal detector — bugs live in comments, dead code, and swallowed errors, none of which are visible in a dependency graph.
```json
{"symbol_id": "crates::wallet::core::close_wallets_batch", "radius": 2, "kinds": ["todo", "error_swallow"]}
```
- Warning kinds: `todo`, `fixme`, `hack`, `xxx`, `commented_code` (3+ commented-out code lines), `error_swallow` (`let _ = fn()` discarding Results), `fail_open` (`unwrap_or_default()` on auth paths), `stub_fn` (function bodies that are only logging).
- Observations are extracted during indexing and attached to the nearest enclosing symbol; the tool joins them against the blast radius.
- Precision-first by design: a missed warning is acceptable, a fabricated one destroys trust.

#### `diff_modules`
Symbol-set drift between counterpart modules — catches sync/async implementations that have grown apart.
```json
{"module_a": "dashboard::product::mis_report", "module_b": "dashboard::product::generators"}
```
- Returns `only_in_a` / `only_in_b` (the drift) plus shared counts and a drift score.

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

#### `make_decision`
Typed judgments from a local decision model (Jev-style System One, `nimble` 9B via Ollama). **Not chat** — send state text plus named questions, get back a `choice`, a `score`, or a calibrated yes/no probability (`noul`) per question.
```json
{
  "state": "PR changes 446 files in the wallet closure batch, touching dormancy reactivation paths",
  "questions": {
    "risk": {"type": "score", "instructions": "How risky is this change?", "criteria": ["low", "medium", "high"]},
    "needs_senior_review": {"type": "noul", "instructions": "Does this need senior review before merge?"},
    "area": {"type": "choice", "instructions": "Which team owns this area?", "criteria": {"wallets": null, "dormancy": null, "payments": null}}
  }
}
```
- Returns: `answers` (per question: the value plus `probabilities` and `confidence`) and the model name.
- **Takes 10-35 seconds per call** (local CPU model, memory-bandwidth-bound). Use it for decisions worth waiting on — PR risk assessment, triage routing, content gating — never per-message.
- Single-flight: one decision at a time; concurrent callers get a retry hint.
- Guards: max 8 questions, 8000-char state, question types `noul`/`score`/`choice` only.
- Read-only: cannot mutate any index or data. Graceful error if the model service is down.
- Latency and call counts appear in Grafana (`mcp_tool_latency_ms{tool="make_decision"}`).

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

### Decision-Model Quality Gate

The quality gate is judged by a **local decision model** — [Ollama](https://ollama.com)'s `/v1/systemone` endpoint running `nimble` (Bespoke Labs' open-source 9B decision model, Jev-style typed decisions) — with a rule-based heuristic as automatic fallback when the model service is down.

**Why a decision model instead of rules?** A form-based gate (length checks, symbol-citation counts, rating thresholds) is a Goodhart target: any LLM writes feedback that passes those checks by default, including when the feedback is wrong or vacuous. The decision model judges the *content*: whether the feedback describes expected-but-missing results — the substance that drives query expansion and symbol boosts. On live samples:

| Feedback type | `specific` | `actionable` | `consistent` | Gate |
|---|---|---|---|---|
| Genuine (cites symbols, names gaps) | 0.997 | 0.992 | 0.867 | accepted |
| Low-effort ("it was fine i guess") | 0.027 | 0.065 | 0.391 | rejected |
| **Adversarial** (fake symbols, "everything perfect", rating 5) | 0.999 | **0.042** | 0.995 | **rejected** |

The adversarial row is the case a form-based gate cannot catch — it has every surface signal of quality but zero informational content. The acceptance rule is a hard gate on `actionable >= 0.5`; `specific` and `consistent` feed the stored quality score.

**Speed** (measured on the reference host — i9-14900K, CPU-only, via the actual integration code path):

| Scenario | Latency | Notes |
|---|---|---|
| Cold start (model load + decision) | **~32s** | first call after container start or keep-alive expiry |
| Warm, compact prompt | **6-10s** | no competing load |
| Warm, long prompt / under embedding load | **20-35s** | memory-bandwidth-bound — scales with prompt tokens, not CPU cores |
| Service down (fallback to heuristic) | **<0.1s** | instant, per-request |
| `tev1` 4B (alternative) | 4-12s | weak separation — scores vacuous feedback 0.56 actionable; rejected |
| Hosted Jev API (reference) | 70-500ms | requires network + paid key |

This latency is acceptable because gating is **async and rare**: it runs in the 5-minute reinforcement loop, only when pending feedback exists (~18 submissions in 3 months of use), capped at 5 entries per evaluation. A decision model must never sit on a hot request path.

**Impact on the loop**: the gate decides which feedback reaches signal extraction (symbol boosts, query expansion) and the rebuild thresholds. Better gating means boost weights track genuine usefulness instead of LLM politeness, and the reinforcement loop can't be steered by form-perfect empty feedback.

**Ops**: `oncall-ollama` container (4 CPU / 8GB limits, `OLLAMA_KEEP_ALIVE=30m`), model volume persisted. `SYSTEMONE_URL` unset → gate disabled, heuristic used. Service down → per-request fallback, logged.

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
        │ 0 say - → boost_weight = +0.87    │
        │ 31 say tag_inventory -            │
        │ 0 say + → boost_weight = -0.86    │
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

**Key point**: the vote smoothing makes hijacking expensive. The formula is `boost_weight = (positive - negative) / (positive + negative + 5)` — Laplace-smoothed and bounded to [-1, 1]: zero votes is neutral, a single vote moves the weight by only 1/6, and it converges toward ±1 as votes accumulate. This means the system gets smarter with each PR review, and no single feedback entry can swing a ranking on its own.

**Honest limits of this guarantee**: `client_id` is self-reported, so one actor *can* submit many votes — auth (see below) gates who can submit at all, but identity is not verified. The quality gate filters noise (too-short, symbol-free submissions), not sophisticated wrong feedback. Treat boost weights as a relevance prior, not ground truth.

**Auth**: all MCP tool calls (including the two feedback tools) require a bearer token (`MCP_AUTH_TOKEN`) — constant-time comparison, per-IP rate limiting with lockout after repeated failures. Without the token, only `/health` responds. The server logs a loud warning at startup if the token is unset.

**What the auto-build actually does**: feedback only affects *query-time* reranking and query expansion — it never changes embeddings. At 5 accepted-but-unconsumed feedbacks, a rerank refresh re-sorts existing results. At 10, a full build runs as a **consolidation checkpoint**: it re-derives graph + semantic indexes from current source, creates a rollback point via the parent-chained build registry, and marks feedback consumed. If the parser or embedder was upgraded since the last build, this is where the new version takes effect.

**Rollback quality scoring — known circularity**: the build-comparison score is derived from agent feedback quality ratings, the same signal that drives reinforcement. It catches gross regressions but cannot independently validate a build. The planned fix is a fixed golden evaluation set (~50-100 queries with known-correct symbols, scored recall@k / MRR on every build) as an independent rollback signal.

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

### Parser limitations (known ceiling)

The extractor is regex-based, which caps result quality in specific ways:

- **Method calls**: `x.send()` can't be resolved to which type's `send` without type inference — the edge lands on the name match or is dropped
- **Macros**: code generated by `derive`, `macro_rules!`, or proc macros is invisible
- **Traits**: calls through trait objects and generics resolve to nothing, or to the wrong impl
- **Imports**: `use ... as` aliases, re-exports, and glob imports break name resolution

Risk scores, blast-radius edge counts, and confidence labels all inherit these errors, and tools cannot tell you when they're affected. Confidence labels reflect extraction certainty (direct call vs. name match), **not** semantic resolution certainty. The long-term fix is rust-analyzer's SCIP/LSIF output (or `syn`) instead of regex — likely a bigger quality gain than the entire reinforcement pipeline.

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
 - `POST /api/index/notify/nightly-sync-failed` — Send nightly-sync failure notification

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
| MCP bearer auth | `BearerTokenAuthMiddleware` on all MCP endpoints — constant-time compare, per-IP lockout after 5 failed attempts/min, `/health` exempt only (configurable via `MCP_AUTH_TOKEN`) |
| Trusted host check | `TrustedHostMiddleware` rejects unknown host headers |
| Rate limiting | Per-IP, per-endpoint-class: index/admin=5/min, mutation=20/min, default=100/min |
| Request size limit | 10MB max request body (413 on exceed) |
| CORS | Configurable allowed origins (default: localhost only) |
| Startup warnings | Both servers log a loud warning if their auth token is unset |

**Tool safety model**: 15 of the 17 MCP tools are strictly read-only (graph queries, semantic search, diff analysis, decision-model judgments — they cannot mutate any index or data). The remaining two — `submit_search_feedback` and `submit_ai_feedback` — are write tools: they record feedback in Postgres and can (indirectly, at accepted-feedback thresholds) trigger index rebuilds. They require the MCP bearer token like everything else, but do not treat them as side-effect-free.

### Rate Limiting

Rate limits are enforced by `RateLimitMiddleware` with sliding 60-second windows:

| Route class | Limit | Examples |
|---|---|---|
| Index/admin | 5 req/min | `/api/index/repository`, `/api/index/semantic/rebuild`, `/api/index/replay` |
| Mutation | 20 req/min | `/api/feedback` |
| Default | 100 req/min | All other routes |

Responses include `X-RateLimit-Remaining` header. 429 responses include `Retry-After`.

---

## Infrastructure

### Structured Logging

All logs are emitted as JSON with trace IDs. The `TraceIdMiddleware` adds a `X-Trace-Id` header to every request and includes it in log output, enabling request tracing across services.

```json
{"timestamp": "2026-07-27T...", "level": "INFO", "trace_id": "abc-123", "message": "..."}
```

### Health & Readiness

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness probe — process is running |
| `GET /ready` | Readiness probe — all backends reachable |
| `GET /api/health` | Detailed health: per-backend status (postgres, redis, weaviate, neo4j) |

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
| `MCP_AUTH_TOKEN` | — | Bearer token for MCP auth (empty = no auth) |
| `API_AUTH_TOKEN` | — | Bearer token for API auth (empty = no auth) |
| `TRUSTED_HOSTS` | `["localhost","127.0.0.1","testserver"]` | Allowed host headers |
| `CORS_ALLOWED_ORIGINS` | `["http://localhost:8000"]` | CORS allowed origins |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_TO` | — | Email notifications (empty = disabled) |
| `OUTBOUND_TIMEOUT_SECONDS` | `30` | Timeout for outbound HTTP calls |
| `OUTBOUND_RETRY_COUNT` | `3` | Retry attempts for transient failures |
| `SEMANTIC_SEARCH_TIMEOUT_SECONDS` | `30` | Weaviate query timeout before fallback |

---

## Operations

### Deployment (GitOps loop)

```bash
git push origin main    # that's it
```

1. **gitops-pull** (cron, 2 min): fetch `origin/main`, fast-forward only — refuses dirty trees and diverged branches with one-shot email alerts, builds and tags the image with the git SHA
2. **auto-deploy** (cron, 1 min): deploys on image diff, defers while indexing is running, verifies health (120s grace), runs the **functional smoke suite**, and only then tags `last-good` — a failed build or failed smoke never ships

Rollback: `docker tag on-call-assistance-backend:last-good on-call-assistance-backend:latest && docker compose up -d backend`

### Post-deploy smoke suite

`scripts/smoke.py` — 11 functional invariants, not just liveness: make_decision actually answers, semantic scores have spread (not flat 1.0), graph stats reconcile, traverse + metal detector + multi-word search work, feedback submissions accepted. The deployer runs it after every deploy and emails failures. Each check pins a failure class that actually happened.

### Contract-drift tests

The unit suite includes tests that pin cross-component contracts so they cannot drift silently: the advertised make_decision state cap must equal the enforced cap, the tick's stats keys must exist in the store's return, and the deployer's indexing-guard regex must match the log lines the indexer actually emits.

### Backups

`scripts/backup-postgres.sh` (cron 01:00): nightly gzipped pg_dump to `backups/postgres/` with 7-day retention. Every dump is restore-tested immediately — restored into a scratch database, key tables counted, dropped. Failures (dump error, empty restore, stale backup >25h) email an alert. Postgres is the source of truth; an untested backup is a hope, not a backup.

### Testing

```bash
./scripts/run-tests.sh          # unit tests in a network-isolated container
```

**Never** run pytest with the compose environment attached — test setup resets index singletons and will wipe live backends.

### Other docs

- `startup.md` — from-scratch setup guide
- `ROADMAP.md` — consolidated backlog with progress
- `AGENTS.md` — PR-review workflow for AI agents
- `docs/architecture.html` — interactive architecture diagram

---

## License

MIT

## Access control (UAM)

Tool calls are governed by a user-action model: every API key maps to a user with roles and a tier.

| Tier | Daily quota | Roles allowed |
|---|---|---|
| free | 200 calls | viewer |
| pro | 5,000 calls | viewer, reviewer, operator |
| enterprise | unlimited | all + admin |

Roles grant tool policies (`viewer` = read/search tools, `reviewer` = diff analysis + feedback, `operator` = + `make_decision`, `admin` = everything). Every `tools/call` is policy-checked, quota-counted, and audited (`uam_audit`). Manage via REST (`/api/uam/users`, admin token) or the Grafana **codegraph-admin** dashboard's control card. Keys are stored hashed; the legacy `MCP_AUTH_TOKEN` bootstraps as an enterprise admin and still works if Postgres is down.

## Rate limiting

Token buckets (capacity 60, refill 1/s per user) — no fixed-window boundary bursts, exact `Retry-After`. Tunable at runtime: `PUT /api/admin/rate-limit?capacity=&refill_per_sec=` or the control card.

## Query cache

Redis, tagged (`graph`, `git`, `tests`), zlib-compressed, TTL hard-capped at 4h. Graph rebuilds and git syncs invalidate exactly the affected tags. Metrics: `query_cache_*` in Prometheus.

## Admin dashboard

Grafana `codegraph-admin` (needs `--profile observability`): tool usage from the audit trail, backend logs (Loki), cache health, alert rules, and a control card for rate limits / cache flush / user creation. The admin token is entered once per browser (localStorage) — never stored in the repo or Grafana.
