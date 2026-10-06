# Security

Supply chain, hardening, access control, and admin surfaces for codegraph-mcp.

## Contents
- [Security measures](#security) — supply chain, container hardening, API security, rate limiting
- [Access control (UAM)](#access-control-uam)
- [Query cache](#query-cache)
- [Admin surfaces](#admin-console) — console, Grafana, break-glass token

---

## Security

### Supply Chain

| Measure | Status |
|---|---|
| **pip hash-pinning** | `requirements.txt` generated with `pip-compile --generate-hashes`, installed with `--require-hashes` |
| **Rustup checksum** | SHA256 checksum verification before executing rustup-init |
| **Model pinning** | Embedding model `BAAI/bge-base-en-v1.5` preloaded at build time |
| **SBOM** | Syft generates SPDX SBOM on every image build (CI) |
| **Image signing** | Cosign keyless signing of built images (CI) |
| **Dependency audit** | `pip-audit` in CI security-scans job |
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

**Tool safety model**: 24 of the 26 MCP tools are strictly read-only (graph queries, semantic search, diff analysis, decision-model judgments — they cannot mutate any index or data). The remaining two — `submit_search_feedback` and `submit_ai_feedback` — are write tools: they record feedback in Postgres and can (indirectly, at accepted-feedback thresholds) trigger index rebuilds. They require the MCP bearer token like everything else, but do not treat them as side-effect-free.

### Rate Limiting

**MCP tool calls** — token buckets per authenticated user (default capacity 60 burst, 1/s sustained refill): no fixed-window boundary bursts, exact `Retry-After` on 429. Runtime-tunable without restart via `PUT /api/admin/rate-limit?capacity=&refill_per_sec=` (new calls within 30s).

**REST API** — per-IP, per-endpoint-class fixed limits (index/admin 5/min, mutations 20/min, default 100/min) plus per-IP auth-failure lockout (5 fails/min). 429 responses include `Retry-After`.

---



---

## Access control (UAM)

Tool calls are governed by a user-action model: every API key maps to a user with roles and a tier.

| Tier | Daily quota | Roles allowed |
|---|---|---|
| free | 200 calls | viewer |
| pro | 5,000 calls | viewer, reviewer, operator |
| enterprise | unlimited | all + admin |

Roles grant tool policies (`viewer` = read/search tools, `reviewer` = diff analysis + feedback, `operator` = + `make_decision`, `admin` = everything). Every `tools/call` is policy-checked, quota-counted, and audited (`uam_audit`). Manage via REST (`/api/uam/users`, admin token) or the admin console at `http://<host>:8000/admin`. Keys are stored hashed (SHA-256). The legacy `MCP_AUTH_TOKEN` is a **break-glass master key**, and its auditing is two-tier by design:

- **Postgres up**: the token resolves to `bootstrap-admin` through normal UAM lookup — every call is policy-checked and audited to `uam_audit` like any user.
- **Postgres down** (the case that would otherwise be unauditable): UAM lookup fails, the token still authenticates, and every tool call is appended to `logs/legacy_token_use.log` on the host (size-rotated) with an email alert (rate-limited to 1/hour). Rotate the token if that alert fires unexpectedly.

## Rate limiting

Token buckets per authenticated user (capacity 60 burst, 1/s sustained refill) — no fixed-window boundary bursts, exact `Retry-After` on 429. Runtime-tunable via the admin console or `PUT /api/admin/rate-limit` (new calls within 30s).

## Query cache

Redis, tagged (`graph`, `git`, `tests`), zlib-compressed, TTL hard-capped at 4h. Graph rebuilds and git syncs invalidate exactly the affected tags. Metrics: `query_cache_*` in Prometheus.

## Admin console

Standalone page served by the backend at `http://<host>:8000/admin` (tailnet/LAN only — the public funnel exposes 8002, not 8000). The admin token is entered at runtime and held in browser memory for the tab's lifetime only — never localStorage, never Grafana.

Capabilities: rate-limit tuning, query-cache stats + tagged flush, usage tables from the audit trail. Grafana's `codegraph-admin` dashboard is read-only panels (usage, logs, cache health); Grafana HTML sanitization is fully enabled — no HTML panels anywhere.
