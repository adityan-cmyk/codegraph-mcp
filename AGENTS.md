# On-Call Graph — PR Review with MCP Tools

You have access to a read-only code dependency graph via the `oncall-graph` MCP server. Use these tools during PR review to understand blast radius, dependencies, and find relevant code.

## Available MCP Tools (16)

| Tool | When to use |
|---|---|
| `search_symbols` | FIRST — find exact symbol_ids by partial name (e.g. "InventoryClient") |
| `search_symbols_enhanced` | When search_symbols returns 0 — searches file paths, short names, fuzzy matching |
| `get_blast_radius` | Get callers, callees, type-users, used-types for a symbol + risk score + usage modes + optional usage_modes_filter |
| `get_blast_radius_detailed` | Same as blast_radius but with confidence scores AND usage modes per edge (high/medium/low) |
| `batch_blast_radius` | Get blast radius for multiple changed symbols at once |
| `get_symbol_content` | Read the full source code of a symbol |
| `semantic_search` | Find code by meaning (e.g. "payment validation logic"). 30s timeout, hybrid BM25+vector, auto-fallback to graph search |
| `analyze_pr_diff` | Parse a git diff, classify change types (new/signature_change/body_only/trait_impl/struct_field/deleted), get blast radius in one call |
| `find_dependency_path` | How does symbol A affect symbol B? Shortest path. Prefers CALLS over USES. |
| `traverse_graph` | Multi-hop impact radius (depth 1-5) from a symbol. Supports summary_only mode. |
| `get_graph_stats` | Total nodes and edges in the graph |
| `get_index_meta` | Graph build metadata — gen, commit hash, classifier version, timestamp. Verify graph freshness. |
| `get_symbols_in_file` | List all symbols defined in a file. Resolve diff file paths to exact symbols. |
| `submit_search_feedback` | After using semantic_search — rate results +1 (helpful), -1 (not helpful), or query-level (no symbol) |
| `submit_ai_feedback` | After completing PR review — submit full analysis summary (tools called, results used, expected, rating, suggestions) |
| `get_reinforcement_stats` | See which symbols are boosted/penalized from past feedback |

All tools are read-only. They cannot modify the index, run code, or access files.

## PR Review Workflow

1. **Verify graph freshness**: Call `get_index_meta` to confirm the graph is up-to-date.
2. **Analyze diff**: Call `analyze_pr_diff` with the full diff text — classifies change types and gets blast radius.
3. **Filter by impact**: Use `get_blast_radius` with `usage_modes_filter=["pattern_match", "construction"]` to find only callers that would break.
4. **Traverse**: If significant, call `traverse_graph` with `depth=2` and `summary_only=true` for compact blast radius size.
5. **File lookup**: Use `get_symbols_in_file` to resolve diff file paths to exact symbols.
6. **Risk assessment** — flag any PR that modifies a symbol with:
   - `risk_score: critical` or `high`
   - >10 downstream callers or >5 upstream callers
   - >20 total connections

## Submitting Feedback (IMPORTANT — DO NOT SKIP)

After completing your PR analysis, you MUST submit feedback via MCP tools so the system improves.

### Step 1: Rate individual search results (optional)

After calling `semantic_search`, if you can identify specific results:

```
submit_search_feedback({
  "query_text": "PPI load reservation flow",
  "symbol_id": "crates::wallet::load::ppi_reservation",
  "feedback": -1,
  "original_score": 0.72,
  "reason": "Not the relevant PPI flow — surfaced a rate limiter instead"
})
```

Or query-level (no specific symbol):
```
submit_search_feedback({
  "query_text": "dormancy customer activity report audit trail",
  "reason": "Expected dormancy report and manual deactivation anchors but they were not surfaced"
})
```

### Step 2: Submit full analysis summary (REQUIRED)

After your entire PR review is done, call this MCP tool with everything:

```
submit_ai_feedback({
  "client_id": "opencode-agent",
  "pr_context": "PR title or branch name",
  "tools_called": [
    {"tool": "search_symbols", "args": {"query": "RedisConfig"}, "result_summary": "resolved crates::common::redis::config::RedisConfig"},
    {"tool": "get_blast_radius", "args": {"symbol_id": "crates::common::redis::config::build_client"}, "result_summary": "shared Redis bootstrap path"}
  ],
  "results_used": [
    {"symbol_id": "crates::common::redis::config::build_client", "file_path": "crates/common/src/redis/config.rs", "helpful": true, "notes": "Best anchor for redis-rs upgrade"}
  ],
  "results_expected": "Expected to find dormancy report and manual deactivation anchors",
  "quality_rating": 3,
  "improvement_suggestions": "Search should match on partial function names, not just semantic meaning"
})
```

This goes through a quality gate (only specific, actionable feedback is accepted). At 10 accepted feedbacks, a new build is auto-triggered.

### 3. Check reinforcement stats (optional)

```
get_reinforcement_stats({})
```

Shows how many symbols are boosted/penalized from accumulated feedback.

## Notes

- The MCP server is **stateless** — no session required. You can call `tools/list` and `tools/call` directly.
- The graph indexes a Rust codebase. Symbol IDs use `::` paths (e.g. `crates::inventory::client::InventoryClient`).
- All 16 tools are available at `http://<host-ip>:8002/mcp` via MCP JSON-RPC.
- The feedback API is at `http://<host-ip>:8000/api/feedback/`.
