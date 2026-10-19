# MCP Tools

The canonical tool table below is **generated from the live registry** —
regenerate with `./scripts/generate-tools-doc.sh` after any tool change, in
the same commit. The unit suite (`test_tools_doc_contract`) fails if this
table and the server's registry disagree, so the reference cannot silently
drift from reality.

Connection: `POST http://<host-ip>:8002/mcp` (MCP JSON-RPC, bearer token),
`GET http://<host-ip>:8002/` for the machine-readable directory.

<!-- BEGIN GENERATED TOOL TABLE -->
Tool schema version: `1.2.0` — 29 tools (27 read-only, 2 feedback-writing).

| Tool | Mode | Description |
|---|---|---|
| `analyze_pr_diff` | read-only | Analyze a git diff and return changed symbols with blast radius in one call. This is a chained tool that: (1) parses the diff for function/struct/trait names, (2) resolves them against the graph, (3) gets blast radius for each resolved symbol, (4) returns a combined impact report. PR mode (default) classifies every symbol as removed/added/renamed/modified: removed and renamed symbols are resolved against the master index (renames report old_symbol_id + renamed_to so you query callers of the OLD name), added symbols are listed as 'not in index'. Use this instead of calling search_symbols + get_blast_radius separately. Provide the full git diff text. |
| `batch_blast_radius` | read-only | Get blast radius for multiple symbols in a single call. Use this when a PR changes several symbols and you need the combined impact. Provide a list of symbol_ids (e.g. ['crates::wallet::transfer', 'crates::wallet::validate']). Optional: summary_only=true returns counts + risk per symbol WITHOUT the full neighbor lists (use when you only need sizes — full lists for 15+ symbols are huge); resolve_names=true accepts partial names and resolves each via search first (saves a separate search_symbols call per name). |
| `diff_modules` | read-only | Diff the symbol sets of two modules — finds drift between counterparts (e.g. a sync report generator vs its async version, a handler and its mirror). Returns symbols present in A but missing from B and vice versa, matched by short name. Use this to catch sync/async implementations that have drifted apart (fields or functions added to one but not the other). Provide module prefixes, e.g. 'dashboard::product::mis_report' and 'dashboard::product::generators'. |
| `find_cycles` | read-only | Find dependency cycles between modules — strongly connected components in the module-level call/type graph. Circular dependencies make changes ripple unpredictably and block clean testing; use this to find refactoring targets. Read-only, safe to call anytime. |
| `find_dead_code` | read-only | Find symbols with zero inbound callers or type-references in the index — cleanup candidates and a parser-accuracy check (false positives usually point to missed edges from dynamic dispatch, macros, or trait objects). Excludes test files, pseudo-symbols, and main/entry functions. Optional: module_prefix to scope, limit (default 50). |
| `find_dependency_path` | read-only | Find the shortest dependency path between two symbols in the graph. Use this to understand how a change to one symbol could affect another. Provide from_symbol and to_symbol (full symbol_ids), and optional max_depth (default 5, max 10). |
| `find_field_access` | read-only | Find readers and writers of a struct field — 'who reads ChargebackTxn.transaction_ref_id' without grep. Scans every indexed symbol body for field accesses: readers touch `.field`, writers assign `.field =` or construct `field: value`. Provide struct_name (e.g. 'ChargebackTxn' — '' to scan all structs) and field (e.g. 'transaction_ref_id'). Returns per-site symbol_id/file/line/snippet so you can jump to the exact access. Read-only, safe to call anytime. |
| `find_hotspots` | read-only | Rank files by combined risk: git churn (commits in last N days) x graph connectivity x missing test coverage. High churn + highly connected + untested = change hotspot — where regressions are most likely and review effort is best spent. Use this to prioritize review and test-writing. Read-only, safe to call anytime. |
| `find_string_producers` | read-only | Find every symbol that produces a string literal containing the given prefix/fragment — catches two producers of the same format disagreeing (a Rust format!("UT-TXN-…") vs a SQL 'TXN-…' literal is exactly the bug class that ships). Searches Rust format!/string literals AND SQL chunks (tables, migrations). Provide pattern (e.g. 'TXN-', 'UT-TXN'). Returns producing symbol_id, the literal, and whether it sits inside format!. Read-only, safe to call anytime. |
| `find_warnings_in_blast_radius` | read-only | Find code warnings (TODO/FIXME/HACK comments, swallowed errors, stub functions, commented-out code, fail-open defaults) in a symbol's blast radius. This is the metal detector: bugs live in comments, dead code, and discarded Results — none visible in the dependency graph. Use during PR review or incident triage on hotspots. Detector kinds include 'dropped_future' (let _ = …await — result discarded), 'error_mask' (map_err(|_e| …) — the error is erased), and 'constant_return' (decision fns that always return the same value, like should_deduct_for_unsettled_transaction -> true). Provide the full symbol_id. Optional: radius (default 2, max 3 — how many dependency hops to include), kinds=['todo','fixme','hack','xxx','commented_code','error_swallow','fail_open','stub_fn','unsafe_block','ffi_boundary','panic_path','unwrap_density','dropped_future','error_mask','constant_return'] to filter. |
| `get_blast_radius` | read-only | Get the immediate blast radius of a symbol — direct callers (CALLS edges), callees, type-references (USES edges), and used types. Callers are functions that CALL this symbol; type-references are symbols that merely mention the type (fields, params) — they are reported separately and never conflated. Use this during PR review to understand the impact of changing a symbol. Provide the full symbol_id (e.g. 'crates::inventory::client::InventoryClient'). Optional: usage_modes_filter=['pattern_match', 'construction'] to return only callers that pattern-match or construct this type; summary_only=true for counts grouped by module + risk score; exclude_shared_state_types=true (default) filters god-types (GlobalState-like, >50 type-references) from the type expansion — set false to see them. |
| `get_blast_radius_detailed` | read-only | Get blast radius with confidence scores on each edge. Confidence: 'high' = direct caller/callee, 'medium' = type reference, 'low' = inferred. Use this when you need to know how reliable each connection is. Provide the full symbol_id. |
| `get_crate_graph` | read-only | Workspace crate dependencies from Cargo.toml — path deps and workspace-inherited deps between crates, with dependents and crate-level cycles. Cross-crate changes are riskier: a crate with many internal dependents has a wider blast radius. Call without arguments for the full workspace summary with cycles; pass a crate name (e.g. 'common') for that crate's dependencies, dependents, and cycle membership. Read-only, safe to call anytime. |
| `get_graph_stats` | read-only | Get current graph index statistics — total nodes and edges. Read-only, safe to call anytime. graph_nodes = real symbols + bare edge-target nodes (unresolved callees — scaffolding); real_symbols counts only extracted symbols (file summaries, module exports, and trivial constants excluded), and excluded_from_graph reconciles those against get_index_meta's total_symbols chunk count. |
| `get_index_meta` | read-only | Get graph build metadata — build timestamp, commit hash, commit drift (commits_behind), coverage stats, gen number, and collection info. Use this to verify the graph is fresh and which classifier version was used. Read-only, safe to call anytime. |
| `get_reinforcement_stats` | read-only | Get statistics about the reinforcement learning system — how much feedback has been collected, which symbols are boosted or penalized, query expansion count, and per-tool win attribution (which tool directly produced each finding across accepted reviews). Read-only, safe to call anytime. |
| `get_symbol_content` | read-only | Get the source code content of a symbol — its full implementation, file path, and line range. Use this after get_blast_radius to understand what a symbol actually does. For 500+ line symbols the content is truncated — the response tells you the total line span, so re-call with start_line/end_line (absolute file line numbers within the symbol's range) to page through the rest. |
| `get_symbols_in_file` | read-only | List all symbols defined in a given file. Use this to resolve a diff's file path to the exact symbols that changed — avoids guessing symbol names. Provide a file path like 'crates/common/src/redis/wrapper.rs'. |
| `get_tests_for_symbol` | read-only | Find the tests that cover a symbol — test fns that CALL it or reference its type (USES), from symbols flagged is_test (#[test]/#[tokio::test]/#[cfg(test)] modules). Use this before changing a symbol to see if existing tests will catch regressions. Transitive coverage matters: a symbol may only be exercised through a wrapper (depth=2 finds tests of its callers). Assertion lines from each matched test are returned so you can see WHAT is verified, not just that a test exists. Provide the full symbol_id (e.g. 'crates::wallet::settle::process_payment'). Optional: depth=1..3 (default 1 — direct tests only; 2 includes tests of direct callers, 3 one more hop), include_assertions=false to skip reading test bodies. Read-only, safe to call anytime. |
| `make_decision` | read-only | Get fast, typed judgments from a local decision model (Jev-style System One). Sends your state text plus named questions and gets back a choice, a score, or a calibrated yes/no probability (noul) for each — NOT chat. Takes 10-35 seconds per call (local CPU model), so use it for decisions worth waiting on: PR risk assessment, triage routing, content gating — not for anything per-message. Provide 'state' (the text/JSON to judge, max 2000 chars). All questions are answered in ONE model pass (~10-60s total, not per question) — set client timeouts to at least 120s and 'questions': an object of up to 8 named questions, each {type: 'noul'|'score'|'choice', instructions: string, criteria: for choice — an object of allowed values; for score — an array of labels low to high}. Example: {"state": "PR changes 446 files in wallet closure", "questions": {"risk": {"type": "score", "instructions": "How risky?", "criteria": ["low", "medium", "high"]}, "needs_review": {"type": "noul", "instructions": "Needs senior review?"}}} |
| `recent_changes_near` | read-only | Commits from the last N days touching a symbol — directly (same file) or through its blast radius (files of 1-hop callers/callees/type-referencers). Use this before changing a symbol to see recent activity and who else has been working nearby. Provide the full symbol_id. Default window: 14 days. Read-only, safe to call anytime. |
| `resolve_stacktrace` | read-only | Map panic backtraces or file:line log entries to graph symbols. Provide raw stacktrace text (any format) — every 'path/to/file.rs:LINE' found is resolved to the innermost enclosing symbol with its location. Use this during incidents to go from a crash to the code neighborhood in one call. Optional: include_neighborhood=true adds a compact blast-radius summary per frame. |
| `search_symbols` | read-only | Search for symbols in the graph by partial name match. Use this FIRST when you don't know the exact symbol_id — it returns matching symbol IDs that you can then pass to get_blast_radius or traverse_graph. Provide a partial name (e.g. 'InventoryClient', 'process_review', 'tag_inventory'). |
| `search_symbols_batch` | read-only | Resolve many partial names in ONE call — same per-name results as search_symbols, but one round trip instead of N. Use this whenever you have a list of names (imports from a diff, field names, call targets). Provide queries: list of partial names (e.g. ['lookup_txn', 'check_if_txn', 'find_by_ref_id']). Optional: limit per query (default 5, max 20). |
| `search_symbols_enhanced` | read-only | Enhanced symbol search with file path matching and fuzzy name resolution. Use this when search_symbols returns 0 matches — it searches file paths, short names, and partial matches. Provide a partial name or file path (e.g. 'deactivate_customer', 'dormancy_report.rs', 'customer_controller'). |
| `semantic_search` | read-only |  |
| `submit_ai_feedback` | mutating (feedback only) | Submit a full post-analysis feedback summary after completing a PR review. This is the PRIMARY feedback mechanism — call this after your entire review is done with all tools you called, which results were helpful, what you expected but didn't find, a quality rating (1-5), and improvement suggestions. Also pass tool_wins: [{tool, finding, symbol_id?}] — attribute each review finding to the tool that directly produced it, so per-tool value can be measured across reviews (an overall rating can't tell which tool earned it). The feedback goes through a quality gate (only specific, actionable feedback is accepted) and at 10 accepted feedbacks, a new build is auto-triggered. This tool replaces the HTTP POST to /api/feedback/ai — use this MCP tool instead. |
| `submit_search_feedback` | mutating (feedback only) | Submit feedback on semantic search results to improve future search quality. Call this after reviewing results from semantic_search. Two modes: (1) Per-symbol: provide symbol_id + feedback (1=helpful, -1=not helpful) to boost/penalize that specific symbol. (2) Query-level: provide only query_text + reason (no symbol_id) to signal that the overall search results for that query were poor — the system logs this as a gap. Always provide query_text. After your full PR review, also submit a summary via submit_ai_feedback. |
| `traverse_graph` | read-only | Traverse the dependency graph from a symbol up to N hops, returning neighborhoods for every reachable symbol. Use this to map the full impact radius of a change across the codebase. Provide symbol_id and optional depth (default 1, max 5). Set summary_only=true for a compact response with just counts per hop — use this when you only need the blast radius size, not the full symbol lists. exclude_shared_state_types=true (default, same as get_blast_radius) filters god-types (GlobalState-like, >50 type-references) from every hop — without it a depth-3 traversal reaches ~40% of the graph through shared-state types and stops meaning anything. |
<!-- END GENERATED TOOL TABLE -->











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
- Returns: `content` (pure source code, max 8000 chars — never injected summaries), `enrichment_summary` (the index-time doc/import summary, separately), `file_path`, `start_line`, `end_line`, `total_lines`, `truncated`, and a paging hint.
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
- Std trait impls (`From`, `Display`, ...) and per-module helper clones are excluded from the drift score (`boilerplate_excluded` lists them).

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
- `exclude_shared_state_types=true` by default (same as `get_blast_radius`): god-types are filtered from every hop and the traversal does not expand through them — set `false` only if you explicitly need the shared-state fanout.
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

