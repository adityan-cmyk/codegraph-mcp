# Operations

Runbooks, internals, and day-to-day operation of the codegraph-mcp stack.

## Contents
- [Reinforcement learning system](#reinforcement-learning-system) — feedback loop, decision-model gate, builds
- [Codebase indexing](#codebase-indexing) — pipeline, SCIP refinement, edge provenance, incremental ingest
- [API endpoints](#api-endpoints)
- [Data safety & recovery](#data-safety--recovery)
- [Infrastructure](#infrastructure) — logging, health, timeouts, ADRs
- [Environment variables](#environment-variables)
- [Day-to-day operations](#operations) — deploy, smoke, backups, testing

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

**Rollback quality scoring — known circularity**: the build-comparison score is derived from agent feedback quality ratings, the same signal that drives reinforcement. It catches gross regressions but cannot independently validate a build. The golden evaluation set is live: every +1 search-feedback row is a verified (query, symbol) pair (~90 pairs, growing with use), replayed through the full pipeline on every build — hit@5 / hit@10 / MRR recorded as the build's `quality_score` with a loud warning on >10% regression vs parent. It is independent of boost weights (never feeds them), though the pairs originate from feedback.

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

1. Regex parser extracts `.rs` file symbols → `CodeChunk` objects (functions, structs, impls, traits, enums; `is_test` from `#[cfg(test)]`/`#[test]`, visibility from `pub`/`pub(crate)`)
2. Call targets extracted via `CALL_PATTERN`, `METHOD_CALL_PATTERN` (kind-aware: only fns/methods are call targets)
3. Type references extracted via `TYPE_REF_PATTERN` (matches `impl X`, `: Type`, `-> Type`, `<Type>`, and `TypeName::Variant`)
4. Usage modes classified at index time: `pattern_match`, `construction`, `field_declaration`, `trait_impl`, `type_param`, `import`, `type_reference`
5. Impl block methods extracted as separate symbols
6. **SCIP refinement** (gen-9+): when a rust-analyzer index exists (`scripts/generate-scip.sh`, enabled via `SCIP_REFINE_ENABLED`), its type-resolved edges REPLACE regex edges for covered files — trait dispatch, imports, and cross-module calls the regex extractor cannot see. Ambiguous symbols are skipped, never guessed.
7. Chunk enrichment: module path prepended to content before embedding
8. Snapshot stored in Postgres (incremental updates, not full re-insert)
9. Graph built in Neo4j (zero-downtime, gen-tagged, usage modes on edges, composite (id,gen) constraint)
10. Semantic index built in Weaviate (zero-downtime, shadow collection)
11. Metal-detector pass: TODOs, swallowed errors, stubs, dead code, panic paths, unsafe/FFI markers → `code_observations`

### Edge provenance and remaining limitations

Since gen-9 the graph mixes two edge qualities, and every edge is labeled:

- **`scip`** — type-resolved by rust-analyzer (SCIP refinement): trait dispatch, imports, re-exports, and cross-module calls resolved exactly; ambiguous references skipped, never guessed. ~933 files / ~5,400 sources covered in gen-9.
- **`regex`** — extractor-guessed for files not covered by the SCIP index.

Per-edge visibility: `get_blast_radius_detailed` returns `via: "scip"|"regex"` on each edge; `get_index_meta` reports the split (`scip_edges` / `regex_edges`). Confidence labels reflect extraction certainty — a `regex` edge through a glob import may be wrong in ways a `scip` edge cannot be.

Remaining ceiling (applies to both extractors):

- **Macros**: code generated by `derive`, `macro_rules!`, or proc macros is invisible (rust-analyzer emits references for some macro kinds only)
- **Dynamic dispatch**: `dyn Trait` call sites resolve to the trait, not the concrete impl that will run
- **SCIP snapshot age**: the index is generated per-revision (`scripts/generate-scip.sh`); if it lags the indexed commit, refinement still applies but reflects the older revision — regenerate after large pulls

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

- `docs/tools.md` — the full MCP tool reference (generated table + usage guide)
- `docs/operations.md` — reinforcement system, indexing, API, runbooks
- `docs/security.md` — supply chain, hardening, UAM, rate limits, admin surfaces
- `startup.md` — from-scratch setup guide
- `AGENTS.md` — PR-review workflow for AI agents
- `docs/architecture.html` — interactive architecture diagram

---

