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
- [ ] Replace regex/tree-sitter parsing with rust-analyzer SCIP output (real type resolution: method calls, trait dispatch, imports) — biggest single quality gain
- [ ] Track symbol visibility (pub vs pub(crate) vs private) and weight it into risk scores
- [ ] Crate-level graph from Cargo.toml workspace members (cross-crate changes = riskier)
- [ ] Handle `#[cfg(...)]` and feature flags (cfg(test) currently inflates blast radius)
- [ ] `cargo-semver-checks` on release branch for certain public-API break detection

### Risk scoring
- [ ] Git history signals: churn, co-change coupling, ownership concentration, fix/revert frequency
- [x] Code risk markers: unsafe blocks, FFI boundaries, unwrap()/expect() density, panic! paths
- [ ] Backtest the score against history (reverts + hotfixes as labels); tune thresholds from data

### New tools
- [x] `get_tests_for_symbol` — which tests reach a symbol ("12 callers, 0 tests") — shipped with risk-score test exclusion + is_test extraction
- [x] `resolve_stacktrace` — map panic backtraces / file:line logs to symbols + neighborhoods
- [x] `recent_changes_near` — commits from last N days touching a symbol or its blast radius
- [x] `find_hotspots` — high-churn + highly-connected + poorly-tested, ranked
- [ ] `find_cycles` — dependency cycles between modules/crates
- [x] `find_dead_code` — no callers outside tests; false positives double as parser-accuracy checks

### Search quality
- [ ] Cross-encoder reranker (bge-reranker-base) over top hybrid results
- [ ] Try a code-specific embedding model vs bge-base on the golden set
- [ ] Include `///` doc comments and signatures prominently in chunks
- [ ] Query-conditioned boosts instead of global (payment-helpful ≠ boosted everywhere)

### Learning loop
- [x] Golden eval set — built from +1 search feedback (no hand-labeling), hit@5/hit@10/MRR via GET /api/index/eval/golden, quality recorded per build in the registry (rollback signal ready; auto-rollback deliberately off until baselines stabilize)
- [ ] Mine implicit feedback from logs (search → get_symbol_content on result #4 = relevance signal)
- [ ] Deterministic feedback validation (cited symbols exist, claimed tool calls appear in logs)

### Agent ergonomics
- [ ] Ship the PR-review workflow as an MCP prompt (travels with the server)
- [ ] Consolidate `_detailed`/`_enhanced` variants into options on base tools
- [ ] Token-budget-aware responses: summary-first, pagination, consistent error codes
- [ ] Version tool schemas + contract tests (response-shape changes shouldn't silently break agents)

### Operations and reliability
- [x] **Postgres backups** — nightly pg_dump to another disk/object storage + tested restore (most important item here; Postgres is the source of truth)
- [ ] Run tests before deploying (deployer already gates `last-good` on health; add the test gate)
- [x] Grafana alerting — provisioned rules: backend down (2m), p95 latency >10s (5m), error rate >5% (5m); SMTP contact point + policies; nightly-sync stays cron+email (log-based rule would double-alert)
- [ ] Docker secrets — deferred: UAM keys are hashed in Postgres, tokens are separate scoped secrets in a 600-mode gitignored .env; compose-secrets migration is a deploy-path risk
- [ ] Neo4j indexes + constraints on symbol IDs and generation tags (traversal speed as graph grows)
- [x] Redis cache — tagged (graph/git/tests), zlib-compressed, TTL hard-capped at 4h, gen-keyed + tag invalidation hooks on git sync and graph swap, Prometheus hit/miss/error metrics, admin flush endpoint
- [x] Load-test — scripts/loadtest.py (concurrent workers, per-tool p50/p95/p99); baseline 2.5 req/s, 0 errors, semantic_search p99 3.9s
- [x] Ollama model pinned — nimble copied to nimble-pinned-24e550a1 (digest-encoded name, blob-shared, immune to re-pulls)

### Adoption
- [ ] Blast-radius summaries as PR comments (GitHub Action / webhook) — needs repo webhooks, deferred
- [x] Lite mode — compose profiles: default = core (backend+postgres+redis+weaviate+neo4j+t2v); --profile ollama; --profile observability; all scripts use explicit service names so profiles are safe
- [ ] Index progress endpoint (the 1-2h first index shouldn't be a black box)


## Session additions (2026-10-03/04)
- [x] UAM (user action model) — IAM-style roles + tool policies + paid-service tiers (free/pro/enterprise), hashed API keys, per-call policy+quota+audit, admin REST, legacy-token bootstrap fallback
- [x] Token-bucket rate limiting (capacity 60, refill 1/s, per-user, runtime-tunable from the admin panel)
- [x] Grafana admin dashboard (codegraph-admin): usage tables from uam_audit, Loki logs, cache panels, control card (rate limit, cache flush, user creation) — admin token lives in browser localStorage only, never in repo or Grafana DB
- [x] Security hardening: separate scoped tokens per surface, constant-time compares, login buckets, grafana_ro restricted to uam_audit (no key hashes), security headers + CSP, CORS scoped to Grafana origin
- [ ] Remaining from Phase 2: SCIP/rust-analyzer parsing (needs their build), crate-level Cargo.toml graph, cross-encoder reranker (CPU-bound), embedding model trial, semver-checks, GitHub Action PR comments
