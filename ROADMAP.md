# On-Call Graph — Roadmap

Consolidated from the 5-session MCP review and the follow-up list.
Phase 1 = review backlog (current work). Phase 2 = next.

## Phase 1 — Review backlog (in progress)

### P0 — Reliability (finish)
- [x] make_decision regression — docs/schema said 8000-char states, code capped at 2000 (`e66e386`)
- [x] Health-check endpoint — `/api/health/decision-model` self-reports model presence
- [x] Honest error messages — validation says exactly what was wrong
- [x] MCP timeout ~30s/question — batch or parallelize questions; document the hard limit

### P1 — Data quality (the graph must not lie)
- [x] Strip generic-param mangling from symbol ids (`_S`, `_C_`, `_F_`, `_T_` monomorphization artifacts) (`92728a4`, reinforcement rebuilt from raw events)
- [x] Phantom high-confidence edges — calibrate confidence on resolved targets, not edge count; deweight hub names
- [x] Separate `called_by` from `referenced_by` in blast radius output (TtumStatus "208 callers" is mostly field refs)
- [x] Index `src/scripts/*` and bins; surface per-file coverage stats (generators/mod.rs was 13/23)
- [x] Show commit drift in `get_index_meta` (commits behind HEAD)
- [x] `exclude_shared_state_types` filter (GlobalState: 449 type-users, 907 reachable — pure noise)

### P2 — Metal detector (bugs live in comments, dead code, swallowed errors)
- [x] TODO/FIXME/HACK indexing + `find_todos_in_blast_radius(symbol)` — highest-value single addition
- [x] Error-swallowing detection: `let _ = fn()` on money paths (7 instances found by grep)
- [x] Commented-out code blocks (disabled dedup checks, dormancy guards)
- [x] Stub/println-function detection (dispute codes 452/474 settle no funds)
- [x] Sync/async counterpart pairing (diff `mis_report::X` vs `generators::X_report` field sets)
- [x] Fail-open defaults: `unwrap_or_default()` on auth headers

### P3 — API design
- [x] `analyze_pr_diff`: scope symbol resolution to the diff's file paths
- [x] `search_symbols_enhanced`: multi-word queries ("notification sms" → 0 results)
- [x] `usage_modes_filter` no-op on call edges (`235b658`)
- [x] `get_symbol_content`: return line ranges, let callers page (truncates ~50% on 500+ line fns)

## Phase 2 — Next list

### Analysis accuracy
- [x] SCIP refinement LIVE — scripts/generate-scip.sh (cached RA binary + registry) -> protobuf ingestor -> type-resolved edges replace regex edges for 933 covered files (gen-9: 5,363 sources switched, 5,344 calls + 3,871 uses from 347k occurrences); ambiguity skipped, never guessed
- [x] Symbol visibility parsed + weighted into risk (private 0.4x / pub(crate) 0.7x effective callers; also fixed the extractor never matching pub(crate) items)
- [x] Crate-level graph — get_crate_graph tool (26): workspace members + path/workspace-inherited deps, dependents, cross-crate risk, crate-level cycles via shared Tarjan
- [x] cfg(test) handled (is_test extraction, risk exclusion, get_tests_for_symbol); non-test feature flags remain open
- [x] semver-checks tooling — scripts/semver-checks.sh (binstall, toolchain override for the repo's stale rust-toolchain.toml pin); CURRENTLY BLOCKED on their code: workspace fails rustdoc under rustc 1.99 (sea_orm ColumnTrait::save_array_as missing) — reported as a finding, needs their toolchain/pin fix

### Risk scoring
- [x] Git history signals: churn + fix/revert frequency + ownership concentration (bus-factor) in find_hotspots, co-change partners in recent_changes_near
- [x] Code risk markers: unsafe blocks, FFI boundaries, unwrap()/expect() density, panic! paths
- [x] Backtest — scripts/backtest_risk.py: precision/recall @10/20/50 of hotspot ranks vs incident files; report-only until more history accumulates

### New tools
- [x] `get_tests_for_symbol` — which tests reach a symbol ("12 callers, 0 tests") — shipped with risk-score test exclusion + is_test extraction
- [x] `resolve_stacktrace` — map panic backtraces / file:line logs to symbols + neighborhoods
- [x] `recent_changes_near` — commits from last N days touching a symbol or its blast radius
- [x] `find_hotspots` — high-churn + highly-connected + poorly-tested, ranked
- [x] `find_cycles` — module-level Tarjan SCC (iterative); crate-level via get_crate_graph
- [x] `find_dead_code` — no callers outside tests; false positives double as parser-accuracy checks

### Search quality
- [x] Cross-encoder reranker LIVE — sidecar container (fast default ms-marco-MiniLM ~150ms/search on CPU; bge available via RERANKER_MODEL env), startup preload, 3.5s budget with hybrid-order fallback, top-8 x 400-char docs
- [x] Embedding model trial — scripts/embedding-trial.py, identical-proxy comparison: MiniLM (live) MRR 0.67 / hit@5 82.8% BEATS jina-code 0.62 / 79.3% — keep MiniLM, no swap justified
- [x] Doc comments prefixed into chunk content at extraction (`// {doc}`) — embedded with the symbol text
- [x] Query-conditioned boosts — similar-past-query weights replace global when available, global as cold-start

### Learning loop
- [x] Golden eval set — built from +1 search feedback (no hand-labeling), hit@5/hit@10/MRR via GET /api/index/eval/golden, quality recorded per build in the registry (rollback signal ready; auto-rollback deliberately off until baselines stabilize)
- [x] Implicit feedback — click-through mining (content lookup on a recently-served result records a labeled +1)
- [x] Deterministic validation — fabricated symbol ids in results_used rejected with the list

### Agent ergonomics
- [x] PR-review workflow as pr_review MCP prompt (prompts/list + prompts/get, diff templating)
- [ ] Consolidate `_detailed`/`_enhanced` variants into options on base tools
- [x] Token-budget responses — summary_only on blast radius/traverse, line-range paging on get_symbol_content, structured UAM/error payloads
- [x] TOOL_SCHEMA_VERSION in tools/list + pinned 26-tool registry contract test

### Operations and reliability
- [x] **Postgres backups** — nightly pg_dump to another disk/object storage + tested restore (most important item here; Postgres is the source of truth)
- [x] Full pytest gate before deploy — proven: it + smoke caught the constraint-syntax deploy (Oct 4 00:42)
- [x] Grafana alerting — provisioned rules: backend down (2m), p95 latency >10s (5m), error rate >5% (5m); SMTP contact point + policies; nightly-sync stays cron+email (log-based rule would double-alert)
- [ ] Docker secrets — deferred: UAM keys are hashed in Postgres, tokens are separate scoped secrets in a 600-mode gitignored .env; compose-secrets migration is a deploy-path risk
- [x] Neo4j composite uniqueness (id,gen) + is_test index + gen index
- [x] Redis cache — tagged (graph/git/tests), zlib-compressed, TTL hard-capped at 4h, gen-keyed + tag invalidation hooks on git sync and graph swap, Prometheus hit/miss/error metrics, admin flush endpoint
- [x] Load-test — scripts/loadtest.py (concurrent workers, per-tool p50/p95/p99); baseline 2.5 req/s, 0 errors, semantic_search p99 3.9s
- [x] Ollama model pinned — nimble copied to nimble-pinned-24e550a1 (digest-encoded name, blob-shared, immune to re-pulls)

### Adoption
- [ ] Blast-radius summaries as PR comments — deferred to the END at user request (endpoint + workflow template were built and reverted; see git history commit before this one)
- [x] Lite mode — compose profiles: default = core (backend+postgres+redis+weaviate+neo4j+t2v); --profile ollama; --profile observability; all scripts use explicit service names so profiles are safe
- [x] GET /api/index/progress — embedding N/total with percent


## Session additions (2026-10-03/04)
- [x] UAM (user action model) — IAM-style roles + tool policies + paid-service tiers (free/pro/enterprise), hashed API keys, per-call policy+quota+audit, admin REST, legacy-token bootstrap fallback
- [x] Token-bucket rate limiting (capacity 60, refill 1/s, per-user, runtime-tunable from the admin panel)
- [x] Grafana admin dashboard (codegraph-admin): usage tables from uam_audit, Loki logs, cache panels, control card (rate limit, cache flush, user creation) — admin token lives in browser localStorage only, never in repo or Grafana DB
- [x] Security hardening: separate scoped tokens per surface, constant-time compares, login buckets, grafana_ro restricted to uam_audit (no key hashes), security headers + CSP, CORS scoped to Grafana origin
- [ ] Remaining (end-phase or user-blocked): PR comments (deferred to end), semver execution (blocked on their toolchain pin)
