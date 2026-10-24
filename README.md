# codegraph-mcp

A stateless **MCP server** for code dependency graph analysis of Rust codebases — blast radius, semantic search, PR diff analysis, a metal detector for bugs hiding in comments and dead code (TODOs, swallowed errors, stubs), and a reinforcement loop that improves search quality from AI agent feedback. **29 tools: 27 read-only analysis (including a local decision-model tool); the two feedback tools write only to feedback tables.**

Since gen-9 the graph is **SCIP-refined**: a rust-analyzer index supplies type-resolved edges (trait dispatch, imports, cross-module calls) wherever available, and every edge carries its provenance (`scip` vs `regex`).

> **Note:** the full on-call assistant (incident lifecycle, LLM agent, chat, eval suites) lives on the [`oncall-assistant`](../../tree/oncall-assistant) branch. This branch is the MCP server only.

**Documentation:** [docs/tools.md](docs/tools.md) — tool reference (generated table + usage guide) · [docs/operations.md](docs/operations.md) — runbooks and internals · [docs/security.md](docs/security.md) — hardening, access control, admin surfaces · [startup.md](startup.md) — from-scratch setup · [AGENTS.md](AGENTS.md) — PR-review workflow for AI agents

---

## Architecture

```
┌──────────────────┐     ┌──────────────────┐     ┌────────────────────┐
│  opencode Agent  │────▶│  MCP Server      │────▶│  Neo4j             │  dependency graph (gen-tagged, SCIP-refined, provenance-labeled edges)
│  (any machine)   │     │  :8002           │────▶│  Weaviate          │  semantic vector search (shadow collections)
│                  │     │  bearer auth +   │────▶│  PostgreSQL        │  snapshots, feedback, build registry
│                  │────▶│  per-IP token    │     │  ollama            │  decision model (nimble, digest-pinned)
│                  │     │  buckets         │     │  Redis             │  query cache (tagged, compressed)
│                  │     │  REST API :8000  │     └────────────────────┘
│                  │     │  (29 tools)      │
└──────────────────┘     └──────────────────┘
```

The backend embeds queries and chunks locally (`BAAI/bge-base-en-v1.5`, 768-dim); Weaviate collections use `Vectorizer.none()` and receive vectors directly — there is no external embedding sidecar.

Observability (internal-only except Grafana): Prometheus (30d TSDB, bearer-token scrape), Loki (30d logs), cAdvisor, and Grafana dashboards (`overview`) — see [docs/security.md](docs/security.md) for the network posture.

An interactive version of this diagram lives in [docs/architecture.html](docs/architecture.html).

### Services

| Service | Purpose | Port | Profile |
|---|---|---|---|
| `backend` | FastAPI REST + stateless MCP server (bearer auth, token-bucket rate limiting, Redis query cache, per-user key form in Grafana) | 8000, 8002 | core |
| `postgres` | Snapshots, feedback, build registry | 5433 | core |
| `redis` | Query cache (tagged, compressed), token buckets | 6380 | core |
| `neo4j` | Code dependency graph (generation-tagged, zero-downtime rebuild) | 7474/7687 | core |
| `weaviate` | Vector search (shadow collections, zero-downtime rebuild) | 8080 | core |
| `reranker` | Cross-encoder rerank sidecar — **opt-in** (`--profile reranker`): measured MRR 0.49→0.395 on the golden set, off by default | — (internal) | reranker |
| `ollama` | Decision model for feedback quality gate (digest-pinned copy) | — | ollama |
| `prometheus` | Metrics TSDB, 10s scrape, 30d retention | — (internal) | observability |
| `loki` | Log aggregation, 30d retention | — (internal) | observability |
| `promtail` | Ships oncall-* container logs to Loki | — | observability |
| `cadvisor` | Per-container CPU/memory metrics | — (internal) | observability |
| `grafana` | Dashboards (read-only panels; the only exposed observability port) | 3000 | observability |

Lite mode: `docker compose up -d` starts core only; add `--profile ollama` / `--profile observability` / `--profile reranker` as needed.

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
POSTGRES_DSN=postgresql://oncall:change-me@localhost:5433/oncall
WEAVIATE_URL=http://localhost:8080
NEO4J_URI=bolt://localhost:7687
MCP_AUTH_TOKEN=your-secret-token
SMTP_HOST=smtp.gmail.com
SMTP_USER=you@gmail.com
SMTP_PASSWORD=your-app-password
SMTP_TO=["you@gmail.com"]
```

Note: `.env` is bash-sourced by cron scripts under `set -euo pipefail` — JSON values must be compact (no spaces after commas).

### 2. Start everything

```bash
docker compose up -d
```

### 3. Access

- API health: `http://localhost:8000/health`
- MCP endpoint: `http://<host-ip>:8002/mcp`
- MCP tool directory: `http://<host-ip>:8002/` (GET)
- Per-user keys: Grafana → **Overview** dashboard → **User keys** form (needs the admin panel token; keys are stored hashed)

---

## MCP Server (Stateless)

The MCP server at `http://<host-ip>:8002/mcp` is **stateless** — no session IDs, no FastMCP. It implements MCP JSON-RPC over HTTP (including `prompts/list` with a structured `pr_review` prompt) using a custom Starlette ASGI app. Each request is self-contained, so server restarts don't break connected clients.

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

All 29 tools, their arguments, and usage guidance: **[docs/tools.md](docs/tools.md)** — the tool table there is generated from the live registry and contract-tested, so it cannot drift from the server.

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

The full workflow (including the server-side `pr_review` prompt) is described in [AGENTS.md](AGENTS.md).

---

## Autofix agent (findings → verified PR)

Agent feedback doesn't just tune rankings — tool-behavior findings become code fixes automatically. Weekly (Mon 06:00, or `scripts/findings-to-pr.sh` on demand), a headless coding agent (`opencode run`) fixes accepted findings and delivers a **verified PR** that a human only reviews and merges.

**Sandbox** — the agent's entire world is a fresh disposable clone under `/home/adi/sandbox/`, deleted after every run. It never touches this checkout, other repos, backups, ssh keys, or `.env` — *reading* `.env` is tracked and forbidden the same as writing it (integrity manifest with atime). Any touch outside the sandbox: alert email, no PR.

**Five gates before a PR exists**:
1. Full unit suite green (host-enforced)
2. **Core contracts** green — contract drift, snapshot round-trip, auth paths, rate limits, registry pin. A core failure means a main-app entity changed → escalation email, no auto-PR, human verifies
3. **Red→green proof** — every `test_fix_*` regression test the agent added must fail on the base commit and pass on the branch
4. **Verifier agent** re-reproduces each finding's reported scenario → `FIXED / STILL BROKEN` verdicts in the PR body
5. Integrity manifest unchanged outside the sandbox

**Hygiene** — the agent cannot push or open PRs (the orchestrator does, after gates); branches are `autofix/<date>`; commits carry code only (agent artifacts stay in local `logs/autofix/`); the PR description is secret-scrubbed, with any config needs under **"Proposed .env additions"** for manual application. Full design: [docs/operations.md](docs/operations.md#autofix-agent-findings--verified-pr).

**Bring your own machine/agent** — nothing about a specific user or agent is hardcoded. Defaults are portable; override per machine:

| Env var | Default | Purpose |
|---|---|---|
| `CODEGRAPH_AGENT_BIN` | `opencode` | coding agent CLI — `claude`, etc. work |
| `CODEGRAPH_AGENT_ARGS` | `run` | args before the prompt (`claude`: `-p`) |
| `CODEGRAPH_SANDBOX` | `~/.cache/codegraph-autofix` | disposable clones live here |
| `CODEGRAPH_REPO` | the repo containing the script | the repo to fix |
| `CODEGRAPH_PROTECTED_PATHS` | repo, `.env`, `backups`, `~/.ssh` | integrity-tracked paths (colon-separated) |
| `CODEGRAPH_CODEBASE_ROOT` | *(unset)* | extra protected path (e.g. the analyzed codebase) |
| `BACKEND_CONTAINER` | `oncall-backend` | findings/email docker container |

---

## Operating the stack

Day-to-day operation is automated: `git push origin main` is the entire deploy command (gitops pull → SHA-tagged image build → test-gated deploy → functional smoke suite → `last-good` tagging). Nightly: incremental git sync, backup with restore-test, digest email. The reinforcement loop gates feedback with a local decision model, applies boosts, and rebuilds indexes zero-downtime.

Runbooks, indexing internals, API endpoints, environment variables, backups, and testing: **[docs/operations.md](docs/operations.md)**.

Security posture, bearer-token access control, rate limiting, and admin surfaces: **[docs/security.md](docs/security.md)**.

---

## License

MIT
