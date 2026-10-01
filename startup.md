# On-Call Graph — Start From Scratch

Step-by-step to run this project on a fresh Linux machine. ~30-45 min end to end
(most of it is waiting for builds and the first index).

## What you get

12 Docker services: backend (API + MCP server), postgres, redis, neo4j, weaviate,
t2v-transformers, ollama (decision model), prometheus, loki, promtail, cadvisor,
grafana, blackbox. Plus 4 cron jobs on the host (nightly sync, watchdog, daily
digest, auto-deploy).

## Prerequisites

- Linux host, Docker Engine + compose plugin (`docker compose version` works)
- ~20 GB free disk (images + models + index data)
- 16 GB RAM minimum (ollama runs a 9B model on CPU; 32 GB+ comfortable)
- The codebase you want to index must be a **git repo on this machine**
  (Rust, parsed with tree-sitter)
- Python 3.12 on the host (only for the watchdog cron and test runner)

## Steps

### 1. Clone + env

```bash
git clone <this-repo> && cd on-call-assistance
cp .env.example .env && chmod 600 .env
```

Edit `.env` — the values that matter:

| Variable | What |
|---|---|
| `CODEBASE_REPO_PATH` | absolute path to the Rust repo you want indexed |
| `CODEBASE_GIT_BRANCH` / `CODEBASE_GIT_REMOTE` | used by nightly sync |
| `MCP_AUTH_TOKEN` | bearer token for MCP tool calls — generate: `openssl rand -hex 32` |
| `API_AUTH_TOKEN` | bearer token for the REST API — same, different value |
| `POSTGRES_DSN` / `NEO4J_PASSWORD` | change default passwords for anything exposed |
| `SMTP_*` | outbound email (digest, deploy + watchdog alerts). Empty = disabled. Gmail needs an app password. |
| `GRAFANA_ADMIN_PASSWORD` / `GRAFANA_PG_PASSWORD` | Grafana login + its read-only Postgres user |
| `SYSTEMONE_URL` / `SYSTEMONE_MODEL` | decision model endpoint. Local default: `http://ollama:11434/v1/systemone`, model `nimble` |

### 2. Pull the decision model

The feedback quality gate and `make_decision` need an ollama model. Start ollama
first, then pull once (models persist in the `ollama_models` volume):

```bash
docker compose up -d ollama
docker exec oncall-ollama ollama pull nimble   # 9B, ~5 GB download
```

Without this the system still runs — the gate falls back to a heuristic and
`make_decision` reports unavailable — but feedback quality gating degrades.

### 3. Start everything

```bash
docker compose up -d --build
```

First build takes a while (torch, transformers). Watch it come up:

```bash
docker compose ps          # wait until backend is (healthy)
docker logs -f oncall-backend
```

### 4. First index

The graph/semantic index is **not** built automatically unless you set
`INDEX_REPLAY_ON_STARTUP` or index-on-startup flags. Trigger it:

```bash
source .env
curl -X POST http://localhost:8000/api/index/repository \
  -H "Authorization: Bearer $API_AUTH_TOKEN" \
  -H "Content-Type: application/json" -d '{}'
```

Expect ~1-2 hours for a large repo on CPU (progress in `docker logs oncall-backend`,
or the Build History panel in Grafana). The index also registers in
`build_registry` (Postgres) when it finishes.

### 5. Verify

```bash
curl http://localhost:8000/health                     # {"status":"ok"}
curl -H "Authorization: Bearer $MCP_AUTH_TOKEN" \
  http://localhost:8000/api/health                    # backend checks: all backends
```

- MCP endpoint: `http://<host>:8000/mcp` (or your funnel URL) with bearer auth
- Grafana: `http://<host>:3000` — login `admin` / `GRAFANA_ADMIN_PASSWORD`,
  dashboard **On-Call Graph Overview** (infra, service health, index, MCP
  traffic, feedback gate, build history, logs)
- Tests: `./scripts/run-tests.sh` — runs pytest in a network-isolated container.
  **Never** run pytest against real backends; test setup wipes indexes.

### 6. Cron jobs (host)

```cron
0 0 * * *  <repo>/scripts/nightly-sync.sh    >> <repo>/logs/nightly-sync.log 2>&1
*/5 * * * * /usr/bin/python3 <repo>/scripts/container-watchdog.py >> <repo>/logs/watchdog.log 2>&1
0 18 * * *  <repo>/scripts/daily-digest.sh   >> <repo>/logs/daily-digest.log 2>&1
* * * * *   <repo>/scripts/auto-deploy.sh    >> <repo>/logs/auto-deploy-cron.log 2>&1
```

Install with `crontab -e` (replace `<repo>` with the absolute path). All API
calls in these scripts read `.env` for `API_AUTH_TOKEN`.

- **nightly-sync** (00:00): pulls latest code, triggers incremental reindex
- **watchdog** (5 min): restarts unhealthy `oncall-*` containers, alerts on loops
- **daily-digest** (18:00): index stats, feedback gate results, improvement
  suggestions, issues backlog — via SMTP
- **auto-deploy** (1 min): deploys whenever the built image differs from the
  running one; tags with git SHA + `last-good`; emails on success/failure

### 7. Deploy workflow (after setup)

```bash
git push && docker compose build backend    # auto-deployer does the rest
```

Rollback: `docker tag on-call-assistance-backend:last-good on-call-assistance-backend:latest && docker compose up -d backend`

## Troubleshooting

- **Backend unhealthy / restarts**: check `docker logs oncall-backend`, watchdog
  log. Healthcheck has a 120 s grace period (`start_period`).
- **Feedback stuck `pending`**: gate calls the local ollama model (~30-60 s per
  entry, 5 per tick). Check `oncall-ollama` is up and `nimble` is pulled.
- **Grafana panels empty**: datasources provision on first start; check
  `oncall-prometheus` / `oncall-loki` logs, and that prometheus token templating
  succeeded (`docker logs oncall-prometheus | grep -i error`).
- **Index stale**: the nightly sync only reindexes when the repo has new commits.
  Manual: the `/api/index/repository` call from step 4.

## More docs

- `README.md` — MCP tools, API, architecture
- `AGENTS.md` — PR-review workflow for AI agents, test-safety rules
- `handoff.md` (gitignored) — ops runbook
