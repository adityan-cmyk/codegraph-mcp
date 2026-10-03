"""Read-only graph MCP tools for opencode agent consumption.

These tools expose ONLY graph read operations and symbol content.
No write, index, file, cargo, or mutation operations are accessible.
"""

import logging
import threading
import time
import uuid

from app.rag.retrieval.graph import graph_index
from app.rag.retrieval.semantic import semantic_index

logger = logging.getLogger(__name__)

_MAX_PER_DIRECTION = 50
_MAX_TRAVERSE_NEIGHBORHOODS = 50
_BOOST_ALPHA = 0.15  # how much learned weights affect final score


def _compute_risk_score(upstream: list[str], downstream: list[str], used_by: list[str], uses: list[str]) -> str:
    total = len(upstream) + len(downstream) + len(used_by) + len(uses)
    callers = len(upstream) + len(used_by)
    if total > 100 or callers > 20:
        return "critical"
    if total > 40 or callers > 10:
        return "high"
    if total > 10 or callers > 3:
        return "medium"
    return "low"


def _compute_risk_factors(upstream: list[str], downstream: list[str], used_by: list[str], uses: list[str]) -> dict[str, object]:
    total = len(upstream) + len(downstream) + len(used_by) + len(uses)
    callers = len(upstream) + len(used_by)
    callees = len(downstream) + len(uses)
    score = _compute_risk_score(upstream, downstream, used_by, uses)

    reasons: list[str] = []
    if callers > 20:
        reasons.append(f"{callers} upstream callers (threshold: 20)")
    if total > 100:
        reasons.append(f"{total} total connections (threshold: 100)")
    if callers > 10 and score != "critical":
        reasons.append(f"{callers} upstream callers (threshold: 10)")
    if total > 40 and score != "critical":
        reasons.append(f"{total} total connections (threshold: 40)")
    if callers > 3 and score in ("medium",):
        reasons.append(f"{callers} upstream callers (threshold: 3)")
    if total > 10 and score in ("medium",):
        reasons.append(f"{total} total connections (threshold: 10)")
    if not reasons:
        reasons.append(f"Low connectivity: {total} connections, {callers} callers")

    return {
        "score": score,
        "total_connections": total,
        "upstream_callers": callers,
        "downstream_callees": callees,
        "formula": "critical: total>100 or callers>20 | high: total>40 or callers>10 | medium: total>10 or callers>3 | low: otherwise",
        "reasons": reasons,
    }


def _get_symbol_metadata(symbol_id: str) -> dict[str, object] | None:
    """Fetch symbol metadata from Postgres snapshot via the semantic index."""
    chunk = semantic_index.get_chunk(symbol_id)
    if chunk:
        return {
            "symbol_id": chunk.symbol_id,
            "kind": chunk.kind,
            "file_path": chunk.file_path,
            "start_line": chunk.start_line,
            "end_line": chunk.end_line,
        }
    from app.core.index_store import index_metadata_store
    snapshot = index_metadata_store.load_snapshot()
    if snapshot:
        for chunk in snapshot.chunks:
            if chunk.symbol_id == symbol_id:
                return {
                    "symbol_id": chunk.symbol_id,
                    "kind": chunk.kind,
                    "file_path": chunk.file_path,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                }
    return None


def _is_noise_symbol(symbol_id: str) -> bool:
    """Filter index noise: wildcard bindings (`_`), single-char scraps, and
    pseudo-symbols (file summaries, module exports, keyword-named parser
    artifacts). Pseudo-symbols kept surfacing as top-ranked results and
    collecting +1 feedback — index artifacts are not rankable code."""
    name = symbol_id.split("::")[-1]
    if name in ("_", "file_summary", "module_exports", "fn", "let", "match", "if", "Relation"):
        return True
    return len(name) <= 1 and not name.isalnum()


def _filter_noise(results: list) -> list:
    return [r for r in results if not _is_noise_symbol(r["symbol_id"] if isinstance(r, dict) else r)]


def search_symbols(query: str, limit: int = 20) -> dict[str, object]:
    """Search for symbols in the graph by partial name match. Use this FIRST when you don't know the exact symbol_id — it returns matching symbol IDs that you can then pass to get_blast_radius or traverse_graph. Provide a partial name (e.g. 'InventoryClient', 'process_review', 'tag_inventory')."""
    limit = max(1, min(limit, 50))
    results = _filter_noise(graph_index.search_symbols(query, limit=max(limit * 2, 50)))[:limit]
    return {
        "query": query,
        "matches": len(results),
        "symbols": results,
    }


_FRESHNESS_CACHE: tuple[float, dict | None] = (0.0, None)
_FRESHNESS_TTL = 300  # seconds


def _stale_index_warning() -> dict | None:
    """Warn when the index is behind repo HEAD — results may be partial.

    Cached for 5 minutes so tool calls don't run git or load the snapshot
    on every request.
    """
    global _FRESHNESS_CACHE
    import time as _t

    now = _t.time()
    if now - _FRESHNESS_CACHE[0] > _FRESHNESS_TTL:
        warning = None
        try:
            from app.rag.ingestion.git_ingestor import get_current_head, get_last_indexed_commit
            head = get_current_head()
            last = get_last_indexed_commit()
            if head and last and head != last:
                warning = {
                    "index_stale": True,
                    "last_indexed_commit": last[:12],
                    "current_head": head[:12],
                    "message": (
                        f"Index is behind HEAD ({last[:8]} vs {head[:8]}) — results may miss "
                        "recently changed or added symbols. Refresh with POST /api/index/ingest."
                    ),
                }
        except Exception:
            logger.debug("Freshness check failed", exc_info=True)
        _FRESHNESS_CACHE = (now, warning)

    return _FRESHNESS_CACHE[1]


def _attach_stale_warning(result: dict) -> dict:
    warning = _stale_index_warning()
    if warning:
        result["index_warning"] = warning
    return result


def _god_type_blocklist() -> set[str]:
    """Types with massive USES fan-in (GlobalState, config structs) — including
    them in blast-radius type expansion turns every query into noise."""
    import time as _time

    global _GOD_TYPES_CACHE
    now = _time.time()
    if _GOD_TYPES_CACHE and now - _GOD_TYPES_CACHE[0] < 300:
        return _GOD_TYPES_CACHE[1]
    try:
        with graph_index._active()._get_driver().session() as session:
            result = session.run(
                """
                MATCH (t:Symbol {gen: $gen})<-[r:USES]-(u:Symbol {gen: $gen})
                WITH t, count(r) AS fan_in
                WHERE fan_in > 50
                RETURN t.id AS id
                """,
                gen=graph_index._active()._gen,
            )
            blocked = {r["id"] for r in result}
    except Exception:
        blocked = set()
    _GOD_TYPES_CACHE = (now, blocked)
    return blocked


_GOD_TYPES_CACHE: tuple[float, set[str]] | None = None


_TEST_SYMBOLS_CACHE: tuple[float, set[str]] = (0.0, set())


def _test_symbol_ids() -> set[str]:
    """Symbol ids flagged is_test (5-min cache) — test callers must not
    inflate production risk scores."""
    global _TEST_SYMBOLS_CACHE
    now = time.monotonic()
    if now - _TEST_SYMBOLS_CACHE[0] > 300:
        try:
            _TEST_SYMBOLS_CACHE = (now, graph_index.get_test_symbol_ids())
        except Exception:
            _TEST_SYMBOLS_CACHE = (now, set())
    return _TEST_SYMBOLS_CACHE[1]


def get_tests_for_symbol(symbol_id: str) -> dict[str, object]:
    """Find the tests that cover a symbol — test fns that CALL it or reference its type (USES), from symbols flagged is_test (#[test]/#[tokio::test]/#[cfg(test)] modules). Use this before changing a symbol to see if existing tests will catch regressions. Provide the full symbol_id (e.g. 'crates::wallet::settle::process_payment'). Read-only, safe to call anytime."""
    symbol_id = symbol_id.strip()
    if not symbol_id:
        return {"error": "symbol_id is required"}

    test_rows = graph_index.get_tests_for(symbol_id)
    direct = [t for t in test_rows if t["relation"] == "calls"]
    mention_only = [t for t in test_rows if t["relation"] == "type_reference"]

    files = sorted({t["file_path"] for t in direct + mention_only if t["file_path"]})
    return {
        "symbol_id": symbol_id,
        "tests_calling": direct,
        "tests_referencing_type": mention_only,
        "test_count": len(direct) + len(mention_only),
        "test_files": files,
        "coverage_note": (
            "direct test callers exist — regressions here are likely caught"
            if direct
            else "no test directly calls this symbol — changes are unverified by the suite"
        ),
    }


def get_blast_radius(symbol_id: str, usage_modes_filter: list[str] | None = None, summary_only: bool = False, exclude_shared_state_types: bool = True) -> dict[str, object]:
    """Get the immediate blast radius of a symbol — direct callers (CALLS edges), callees, type-references (USES edges), and used types. Callers are functions that CALL this symbol; type-references are symbols that merely mention the type (fields, params) — they are reported separately and never conflated. Use this during PR review to understand the impact of changing a symbol. Provide the full symbol_id (e.g. 'crates::inventory::client::InventoryClient'). Optional: usage_modes_filter=['pattern_match', 'construction'] to return only callers that pattern-match or construct this type; summary_only=true for counts grouped by module + risk score; exclude_shared_state_types=true (default) filters god-types (GlobalState-like, >50 type-references) from the type expansion — set false to see them."""
    if not graph_index.has_symbol(symbol_id):
        return {"error": f"Symbol '{symbol_id}' not found in the graph index.", "symbol_id": symbol_id, "hint": "Use search_symbols to find the correct symbol_id."}

    neighborhood = (
        graph_index.get_blast_radius(symbol_id)
        if hasattr(graph_index, "get_blast_radius")
        else graph_index.get_neighbors(symbol_id)
    )
    result = neighborhood.model_dump() if hasattr(neighborhood, "model_dump") else neighborhood

    upstream = result.get("upstream", [])
    downstream = result.get("downstream", [])
    used_by = result.get("used_by", [])
    uses = result.get("uses", [])

    # God-type filter: drop shared-state types from the USES expansion.
    if exclude_shared_state_types:
        blocked = _god_type_blocklist()
        if blocked:
            used_by = [s for s in used_by if s not in blocked]
            uses = [s for s in uses if s not in blocked]

    # Explicit semantics: direct callers vs type references, never conflated.
    result["callers_direct_count"] = len(upstream)
    result["type_reference_count"] = len(used_by)
    result["connection_breakdown"] = {
        "callers_of_this_symbol": len(upstream),
        "called_by_this_symbol": len(downstream),
        "type_references_to_this_symbol": len(used_by),
        "types_used_by_this_symbol": len(uses),
    }

    result["total_upstream"] = len(upstream)
    result["total_downstream"] = len(downstream)
    result["total_used_by"] = len(used_by)
    result["total_uses"] = len(uses)
    result["total_connections"] = len(upstream) + len(downstream) + len(used_by) + len(uses)
    # Risk is a PRODUCTION-blast-radius measure: test callers break loudly and
    # on purpose — they must not inflate the risk of the code under test.
    tests = _test_symbol_ids()
    result["test_caller_count"] = sum(1 for u in upstream if u in tests)
    prod_upstream = [u for u in upstream if u not in tests]
    prod_used_by = [u for u in used_by if u not in tests]
    # Visibility weighting: a private symbol's blast radius is contained by
    # its module; a pub symbol's is not. Scale effective callers accordingly.
    chunk = _symbol_chunk(symbol_id)
    visibility = getattr(chunk, "visibility", "pub") if chunk else "pub"
    result["visibility"] = visibility
    vis_factor = {"private": 0.4, "pub_crate": 0.7, "pub": 1.0}.get(visibility, 1.0)
    eff_upstream = round(len(prod_upstream) * vis_factor)
    eff_used_by = round(len(prod_used_by) * vis_factor)
    result["risk_score"] = _compute_risk_score(
        ["x"] * eff_upstream, downstream, ["x"] * eff_used_by, uses
    )
    result["risk_factors"] = _compute_risk_factors(
        ["x"] * eff_upstream, downstream, ["x"] * eff_used_by, uses
    )
    if vis_factor < 1.0:
        result["risk_factors"]["visibility_relief"] = (
            f"{visibility} symbol — blast radius contained by its module "
            f"(effective callers {eff_upstream + eff_used_by} of {len(prod_upstream) + len(prod_used_by)})"
        )

    meta = _get_symbol_metadata(symbol_id)
    if meta:
        result["kind"] = meta["kind"]
        result["file_path"] = meta["file_path"]
        result["start_line"] = meta["start_line"]
        result["end_line"] = meta["end_line"]

    if summary_only:
        def _by_module(ids: list[str]) -> dict[str, int]:
            counts: dict[str, int] = {}
            for sid in ids:
                parts = sid.split("::")
                mod = "::".join(parts[:3]) if len(parts) > 3 else ("::".join(parts[:-1]) or sid)
                counts[mod] = counts.get(mod, 0) + 1
            return dict(sorted(counts.items(), key=lambda kv: -kv[1])[:12])

        result["upstream_by_module"] = _by_module(upstream)
        result["downstream_by_module"] = _by_module(downstream)
        result["used_by_by_module"] = _by_module(used_by)
        result["uses_by_module"] = _by_module(uses)
        for key in ("upstream", "downstream", "used_by", "uses", "used_by_modes", "uses_modes"):
            result.pop(key, None)
        return _attach_stale_warning(result)

    # Dedup preserving order — parallel edges (same target, different usage
    # modes) must not produce duplicate entries in the flat lists
    result["upstream"] = list(dict.fromkeys(upstream))[:_MAX_PER_DIRECTION]
    result["downstream"] = list(dict.fromkeys(downstream))[:_MAX_PER_DIRECTION]
    result["used_by"] = list(dict.fromkeys(used_by))[:_MAX_PER_DIRECTION]
    result["uses"] = list(dict.fromkeys(uses))[:_MAX_PER_DIRECTION]
    result["used_by_modes"] = result.get("used_by_modes", {})
    result["uses_modes"] = result.get("uses_modes", {})

    if usage_modes_filter:
        used_by_modes = result.get("used_by_modes", {})
        uses_modes = result.get("uses_modes", {})
        # Filter from the FULL mode maps (not the truncated top-N lists) so
        # the filter actually narrows to matching edges across the whole
        # neighborhood, and keep the maps consistent with the flat lists.
        result["used_by"] = sorted(
            s for s, modes in used_by_modes.items()
            if any(m in modes for m in usage_modes_filter)
        )[:_MAX_PER_DIRECTION]
        result["uses"] = sorted(
            s for s, modes in uses_modes.items()
            if any(m in modes for m in usage_modes_filter)
        )[:_MAX_PER_DIRECTION]
        result["used_by_modes"] = {s: m for s, m in used_by_modes.items() if s in set(result["used_by"])}
        result["uses_modes"] = {s: m for s, m in uses_modes.items() if s in set(result["uses"])}
        result["filtered_by_usage_modes"] = {
            "requested": usage_modes_filter,
            "used_by_matches": sum(
                1 for s, m in used_by_modes.items() if any(x in m for x in usage_modes_filter)
            ),
            "uses_matches": sum(
                1 for s, m in uses_modes.items() if any(x in m for x in usage_modes_filter)
            ),
        }

    usage_modes: dict[str, int] = {}
    for mode_list in list(result.get("used_by_modes", {}).values()) + list(result.get("uses_modes", {}).values()):
        for mode in mode_list:
            usage_modes[mode] = usage_modes.get(mode, 0) + 1
    result["usage_mode_summary"] = usage_modes

    return _attach_stale_warning(result)


def batch_blast_radius(symbol_ids: list[str]) -> dict[str, object]:
    """Get blast radius for multiple symbols in a single call. Use this when a PR changes several symbols and you need the combined impact. Provide a list of symbol_ids (e.g. ['crates::wallet::transfer', 'crates::wallet::validate'])."""
    results: list[dict[str, object]] = []
    all_downstream: set[str] = set()
    all_upstream: set[str] = set()
    all_uses: set[str] = set()
    all_used_by: set[str] = set()

    for sid in symbol_ids:
        br = get_blast_radius(sid)
        if "error" in br:
            results.append(br)
            continue
        for direction_key in ("upstream", "downstream", "used_by", "uses"):
            for edge in br.get(direction_key, []):
                # Lists carry plain symbol_id strings; tolerate dict form too
                edge_sid = edge["symbol_id"] if isinstance(edge, dict) else edge
                if not edge_sid:
                    continue
                target_set = {"upstream": all_upstream, "downstream": all_downstream, "uses": all_uses, "used_by": all_used_by}[direction_key]
                target_set.add(edge_sid)
        results.append({
            "symbol_id": br["symbol_id"],
            "risk_score": br["risk_score"],
            "risk_factors": br.get("risk_factors", {}),
            "total_connections": br["total_connections"],
            "usage_mode_summary": br.get("usage_mode_summary", {}),
            "upstream": br["upstream"],
            "downstream": br["downstream"],
            "used_by": br["used_by"],
            "uses": br["uses"],
        })

    changed_set = set(symbol_ids)
    combined_downstream = all_downstream - changed_set
    combined_upstream = all_upstream - changed_set
    combined_uses = all_uses - changed_set
    combined_used_by = all_used_by - changed_set

    return {
        "queried_symbols": len(symbol_ids),
        "results": results,
        "combined_impact": {
            "total_unique_callers": len(combined_upstream),
            "total_unique_callees": len(combined_downstream),
            "total_unique_type_users": len(combined_used_by),
            "total_unique_types_used": len(combined_uses),
            "combined_risk_score": _compute_risk_score(
                list(combined_upstream), list(combined_downstream),
                list(combined_used_by), list(combined_uses),
            ),
            "all_callers": sorted(combined_upstream)[:_MAX_PER_DIRECTION],
            "all_callees": sorted(combined_downstream)[:_MAX_PER_DIRECTION],
            "all_type_users": sorted(combined_used_by)[:_MAX_PER_DIRECTION],
            "all_types_used": sorted(combined_uses)[:_MAX_PER_DIRECTION],
        },
    }


_RECENT_SEARCHES: dict[str, dict] = {}


def _record_click_through(symbol_id: str) -> None:
    """A get_symbol_content call on a recently-served search result is an
    implicit positive signal — mine it into the feedback store."""
    now = time.monotonic()
    for entry in list(_RECENT_SEARCHES.values()):
        if now - entry["at"] > 600:
            continue
        if symbol_id in entry["results"][:10]:
            try:
                from app.rag.reinforcement.feedback_store import record_feedback
                record_feedback(
                    query_text=entry["query"],
                    symbol_id=symbol_id,
                    feedback=1,
                    reason="implicit: content lookup on search result (click-through)",
                    original_score=0.5,
                )
            except Exception:
                pass
            _RECENT_SEARCHES.pop(next((k for k, v in _RECENT_SEARCHES.items() if v is entry), None), None)
            return


def get_symbol_content(symbol_id: str, start_line: int | None = None, end_line: int | None = None) -> dict[str, object]:
    """Get the source code content of a symbol — its full implementation, file path, and line range. Use this after get_blast_radius to understand what a symbol actually does. For 500+ line symbols the content is truncated — the response tells you the total line span, so re-call with start_line/end_line (absolute file line numbers within the symbol's range) to page through the rest."""
    meta = _get_symbol_metadata(symbol_id)
    if not meta:
        return {"error": f"Symbol '{symbol_id}' not found in the index.", "symbol_id": symbol_id, "hint": "Use search_symbols to find the correct symbol_id."}

    _record_click_through(symbol_id)

    from app.core.index_store import index_metadata_store
    snapshot = index_metadata_store.load_snapshot()
    if snapshot:
        for chunk in snapshot.chunks:
            if chunk.symbol_id == symbol_id:
                if start_line is not None or end_line is not None:
                    lo = max(start_line or chunk.start_line, chunk.start_line)
                    hi = min(end_line or chunk.end_line, chunk.end_line)
                    if lo > hi:
                        return {"error": f"line range [{lo}, {hi}] outside symbol range [{chunk.start_line}, {chunk.end_line}]", "symbol_id": symbol_id}
                    page_lines = chunk.content.split("\n")[(lo - chunk.start_line):(hi - chunk.start_line + 1)]
                    return {
                        "symbol_id": chunk.symbol_id,
                        "kind": chunk.kind,
                        "file_path": chunk.file_path,
                        "start_line": lo,
                        "end_line": hi,
                        "symbol_span": [chunk.start_line, chunk.end_line],
                        "total_lines": chunk.end_line - chunk.start_line + 1,
                        "content": "\n".join(page_lines)[:16000],
                        "truncated": (hi - lo + 1) > 16000 // 40,
                    }
                return {
                    "symbol_id": chunk.symbol_id,
                    "kind": chunk.kind,
                    "file_path": chunk.file_path,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "total_lines": chunk.end_line - chunk.start_line + 1,
                    "content": chunk.content[:8000],
                    "truncated": len(chunk.content) > 8000,
                    "hint": "content truncated — re-call with start_line/end_line to page" if len(chunk.content) > 8000 else None,
                }

    return {"error": f"Symbol '{symbol_id}' found in metadata but content not available.", "symbol_id": symbol_id}




def submit_search_feedback(query_text: str, symbol_id: str = "", feedback: int = 0, original_score: float = 0.0, reason: str = "") -> dict[str, object]:
    """Submit feedback on semantic search results to improve future search quality. Call this after reviewing results from semantic_search. Two modes: (1) Per-symbol: provide symbol_id + feedback (1=helpful, -1=not helpful) to boost/penalize that specific symbol. (2) Query-level: provide only query_text + reason (no symbol_id) to signal that the overall search results for that query were poor — the system logs this as a gap. Always provide query_text. After your full PR review, also submit a summary via submit_ai_feedback."""
    if feedback not in (1, -1, 0):
        return {"error": "feedback must be 1 (helpful), -1 (not helpful), or 0 (query-level only)"}

    try:
        from app.rag.reinforcement import feedback_store

        if symbol_id and feedback in (1, -1):
            feedback_store.record_feedback(
                query_text=query_text,
                symbol_id=symbol_id,
                original_score=original_score,
                feedback=feedback,
                reason=reason or None,
            )
            return {
                "status": "recorded",
                "mode": "per_symbol",
                "query_text": query_text,
                "symbol_id": symbol_id,
                "feedback": feedback,
            }
        else:
            feedback_store.record_feedback(
                query_text=query_text,
                symbol_id=f"_query_level:{query_text[:80]}",
                original_score=0.0,
                feedback=-1,
                reason=f"Query-level negative feedback: {reason}" if reason else "Query-level negative feedback (no specific symbol)",
            )
            return {
                "status": "recorded",
                "mode": "query_level",
                "query_text": query_text,
                "note": "Logged as query-level gap. Also POST to /api/feedback/ai with full analysis for build-triggering feedback.",
            }
    except Exception as exc:
        return {"error": str(exc)}


def get_reinforcement_stats() -> dict[str, object]:
    """Get statistics about the reinforcement learning system — how much feedback has been collected, which symbols are boosted or penalized, and query expansion count. Read-only, safe to call anytime."""
    try:
        from app.rag.reinforcement import feedback_store
        return feedback_store.get_reinforcement_stats()
    except Exception as exc:
        return {"error": str(exc)}


def submit_ai_feedback(
    client_id: str = "",
    pr_context: str = "",
    tools_called: list[dict] | None = None,
    results_used: list[dict] | None = None,
    results_expected: str = "",
    quality_rating: int = 0,
    improvement_suggestions: str = "",
) -> dict[str, object]:
    """Submit a full post-analysis feedback summary after completing a PR review. This is the PRIMARY feedback mechanism — call this after your entire review is done with all tools you called, which results were helpful, what you expected but didn't find, a quality rating (1-5), and improvement suggestions. The feedback goes through a quality gate (only specific, actionable feedback is accepted) and at 10 accepted feedbacks, a new build is auto-triggered. This tool replaces the HTTP POST to /api/feedback/ai — use this MCP tool instead."""
    try:
        from app.rag.reinforcement import ai_feedback_store
        return ai_feedback_store.submit_feedback(
            client_id=client_id or None,
            pr_context=pr_context or None,
            tools_called=tools_called or [],
            results_used=results_used or [],
            results_expected=results_expected or None,
            quality_rating=quality_rating if 1 <= quality_rating <= 5 else None,
            improvement_suggestions=improvement_suggestions or None,
        )
    except Exception as exc:
        return {"error": str(exc)}


_PATH_FILTER_SUFFIXES = ("GlobalState", "FastagGlobalState", "AppState", "Config", "Settings", "State", "Arc")

def _is_config_type(symbol_id: str) -> bool:
    short = symbol_id.split("::")[-1]
    return any(short == s or short.endswith(s) for s in _PATH_FILTER_SUFFIXES)


def find_dependency_path(from_symbol: str, to_symbol: str, max_depth: int = 5) -> dict[str, object]:
    """Find the shortest dependency path between two symbols in the graph. Use this to understand how a change to one symbol could affect another. Provide from_symbol and to_symbol (full symbol_ids), and optional max_depth (default 5, max 10)."""
    max_depth = max(1, min(max_depth, 10))

    if not graph_index.has_symbol(from_symbol):
        return {"error": f"Symbol '{from_symbol}' not found.", "hint": "Use search_symbols to find the correct symbol_id."}
    if not graph_index.has_symbol(to_symbol):
        return {"error": f"Symbol '{to_symbol}' not found.", "hint": "Use search_symbols to find the correct symbol_id."}

    backend = graph_index._active()
    if not hasattr(backend, "_get_driver"):
        paths = _find_path_bfs(from_symbol, to_symbol, max_depth)
        if not paths:
            return {"from": from_symbol, "to": to_symbol, "path_found": False, "message": "No path found within max_depth."}
        return {
            "from": from_symbol,
            "to": to_symbol,
            "path_found": True,
            "path_length": len(paths) - 1,
            "path": paths,
        }

    gen = backend._gen
    driver = backend._get_driver()
    with driver.session() as session:
        # DIRECTED traversal only — the undirected '-' form routed paths
        # backwards through call edges, making distant pairs "reachable"
        # via reversed hops (three independent reviewer instances).
        # Direction 1: from_symbol's dependency chain reaches to_symbol
        # (from calls ... calls to).
        result = session.run(
            f"""
            MATCH path = shortestPath(
                (start:Symbol {{id: $from_id, gen: $gen}})-[:CALLS*1..{max_depth}]->(end:Symbol {{id: $to_id, gen: $gen}})
            )
            RETURN [node in nodes(path) | node.id] AS symbol_path,
                   [rel in relationships(path) | type(rel)] AS edge_types
            """,
            from_id=from_symbol,
            to_id=to_symbol,
            gen=gen,
        )
        record = result.single()
        direction = "forward"  # from_symbol depends (transitively) on to_symbol

        if not record:
            # Direction 2: to_symbol's dependency chain reaches from_symbol
            # (a change to from_symbol affects to_symbol's callers).
            result = session.run(
                f"""
                MATCH path = shortestPath(
                    (start:Symbol {{id: $to_id, gen: $gen}})-[:CALLS*1..{max_depth}]->(end:Symbol {{id: $from_id, gen: $gen}})
                )
                RETURN [node in nodes(path) | node.id] AS symbol_path,
                       [rel in relationships(path) | type(rel)] AS edge_types
                """,
                from_id=from_symbol,
                to_id=to_symbol,
                gen=gen,
            )
            record = result.single()
            direction = "reverse"  # to_symbol depends on from_symbol

        if not record:
            result = session.run(
                f"""
                MATCH path = shortestPath(
                    (start:Symbol {{id: $from_id, gen: $gen}})-[:CALLS|USES*1..{max_depth}]->(end:Symbol {{id: $to_id, gen: $gen}})
                )
                WHERE ALL(n IN nodes(path) WHERE NOT n.id ENDS WITH 'GlobalState' AND NOT n.id ENDS WITH 'FastagGlobalState' AND NOT n.id ENDS WITH 'AppState')
                RETURN [node in nodes(path) | node.id] AS symbol_path,
                       [rel in relationships(path) | type(rel)] AS edge_types
                """,
                from_id=from_symbol,
                to_id=to_symbol,
                gen=gen,
            )
            record = result.single()
            direction = "forward"

    if not record:
        return {"from": from_symbol, "to": to_symbol, "path_found": False, "message": f"No path found within {max_depth} hops."}

    symbol_path = record["symbol_path"]
    edge_types = record["edge_types"]

    return {
        "from": from_symbol,
        "to": to_symbol,
        "path_found": True,
        "direction": direction,
        "direction_meaning": (
            "forward: from_symbol's call chain reaches to_symbol (from depends on to)"
            if direction == "forward"
            else "reverse: to_symbol's call chain reaches from_symbol (to depends on from — a change to from affects to)"
        ),
        "path_length": len(symbol_path) - 1,
        "path": symbol_path,
        "edge_types": edge_types,
        "readable_path": " -> ".join(symbol_path),
    }


def _find_path_bfs(from_symbol: str, to_symbol: str, max_depth: int) -> list[str] | None:
    """BFS pathfinding for in-memory graph backends."""
    from collections import deque
    queue = deque([(from_symbol, [from_symbol])])
    visited = {from_symbol}
    while queue:
        current, path = queue.popleft()
        if len(path) - 1 >= max_depth:
            continue
        neighborhood = graph_index.get_blast_radius(current)
        neighbors = neighborhood.upstream + neighborhood.downstream + neighborhood.used_by + neighborhood.uses
        for neighbor in neighbors:
            if neighbor == to_symbol:
                return path + [neighbor]
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, path + [neighbor]))
    return None


def traverse_graph(symbol_id: str, depth: int = 1, summary_only: bool = False) -> dict[str, object]:
    """Traverse the dependency graph from a symbol up to N hops, returning neighborhoods for every reachable symbol. Use this to map the full impact radius of a change across the codebase. Provide symbol_id and optional depth (default 1, max 5). Set summary_only=true for a compact response with just counts per hop — use this when you only need the blast radius size, not the full symbol lists."""
    depth = max(1, min(depth, 5))

    if not graph_index.has_symbol(symbol_id):
        return {"error": f"Symbol '{symbol_id}' not found in the graph index.", "symbol_id": symbol_id, "hint": "Use search_symbols to find the correct symbol_id."}

    # summary_only only needs counts, not full neighborhoods — allow a much
    # larger traversal than the payload-protecting default cap.
    max_hoods = 1000 if summary_only else _MAX_TRAVERSE_NEIGHBORHOODS
    neighborhoods = graph_index.traverse(symbol_id, depth=depth, max_neighborhoods=max_hoods)

    all_symbols: set[str] = set()
    capped_neighborhoods: list[dict] = []
    for n in neighborhoods[:_MAX_TRAVERSE_NEIGHBORHOODS]:
        nb = n.model_dump() if hasattr(n, "model_dump") else n
        all_symbols.update(nb.get("upstream", []))
        all_symbols.update(nb.get("downstream", []))
        all_symbols.update(nb.get("used_by", []))
        all_symbols.update(nb.get("uses", []))
        nb["upstream"] = nb.get("upstream", [])[:_MAX_PER_DIRECTION]
        nb["downstream"] = nb.get("downstream", [])[:_MAX_PER_DIRECTION]
        nb["used_by"] = nb.get("used_by", [])[:_MAX_PER_DIRECTION]
        nb["uses"] = nb.get("uses", [])[:_MAX_PER_DIRECTION]
        capped_neighborhoods.append(nb)

    total_neighborhoods = len(neighborhoods)
    # >= not >: the BFS loop stops at exactly the cap with a possibly
    # non-empty queue — that state IS truncated.
    truncated = total_neighborhoods >= max_hoods

    if summary_only:
        # Counts per BFS hop level (the doc contract): rebuild levels from
        # the full neighborhood adjacency. new_symbols is deduplicated per
        # level; discoveries_by_relation counts edge discoveries (a symbol
        # reachable via two relations from the frontier counts once in
        # new_symbols, twice in discoveries).
        adjacency: dict[str, dict[str, list[str]]] = {}
        for n in neighborhoods:
            nb = n.model_dump() if hasattr(n, "model_dump") else n
            adjacency[nb.get("symbol_id", "")] = {
                "upstream": nb.get("upstream", []),
                "downstream": nb.get("downstream", []),
                "used_by": nb.get("used_by", []),
                "uses": nb.get("uses", []),
            }
        frontier = {symbol_id}
        visited = {symbol_id}
        levels: list[dict] = []
        for level in range(1, depth + 1):
            next_frontier: set[str] = set()
            discoveries = {"upstream": 0, "downstream": 0, "used_by": 0, "uses": 0}
            for sym in frontier:
                rels = adjacency.get(sym) or {}
                for rel, targets in rels.items():
                    fresh = [t for t in targets if t not in visited]
                    discoveries[rel] += len(fresh)
                    next_frontier.update(fresh)
            visited |= next_frontier
            levels.append({
                "hop": level,
                "new_symbols": len(next_frontier),
                "cumulative_reachable": len(visited) - 1,
                "discoveries_by_relation": discoveries,
            })
            frontier = next_frontier
            if not frontier:
                break
        return {
            "root_symbol": symbol_id,
            "depth": depth,
            "symbols_visited": total_neighborhoods,
            "total_reachable_symbols": len(visited) - 1,
            "truncated": truncated,
            "count_definitions": {
                "symbols_visited": "symbols whose neighborhoods were traversed (capped)",
                "total_reachable_symbols": "unique symbols reachable within depth, excluding the root (lower bound when truncated=true)",
            },
            "summary": levels,
        }

    return {
        "root_symbol": symbol_id,
        "depth": depth,
        "symbols_visited": total_neighborhoods,
        "total_reachable_symbols": len(all_symbols),
        "neighborhoods_returned": len(capped_neighborhoods),
        "truncated": truncated,
        "neighborhoods": capped_neighborhoods,
    }


def get_graph_stats() -> dict[str, object]:
    """Get current graph index statistics — total nodes and edges. Read-only, safe to call anytime. Note: graph_nodes counts only symbols eligible for the dependency graph (file summaries, module exports, and trivial constants are excluded) — get_index_meta's total_symbols counts every indexed chunk, so graph_nodes < total_symbols is expected, not drift."""
    stats = dict(graph_index.get_stats())
    try:
        from app.core.index_store import index_metadata_store
        snapshot = index_metadata_store.load_snapshot()
        if snapshot:
            stats["total_symbols_all_chunks"] = len(snapshot.chunks)
            stats["excluded_from_graph"] = len(snapshot.chunks) - stats.get("graph_nodes", len(snapshot.chunks))
            stats["exclusion_note"] = "file summaries, module exports, trivial constants, keyword-named parser artifacts"
    except Exception:
        pass
    return stats


# ============================================================================
# Phase 2: git-history-aware tools
# ============================================================================

def _symbol_chunk(symbol_id: str):
    from app.core.index_store import index_metadata_store
    snapshot = index_metadata_store.load_snapshot()
    if not snapshot:
        return None
    for chunk in snapshot.chunks:
        if chunk.symbol_id == symbol_id:
            return chunk
    return None


def recent_changes_near(symbol_id: str, days: int = 14, include_blast_radius: bool = True) -> dict[str, object]:
    """Commits from the last N days touching a symbol — directly (same file) or through its blast radius (files of 1-hop callers/callees/type-referencers). Use this before changing a symbol to see recent activity and who else has been working nearby. Provide the full symbol_id. Default window: 14 days. Read-only, safe to call anytime."""
    symbol_id = symbol_id.strip()
    if not symbol_id:
        return {"error": "symbol_id is required"}
    days = max(1, min(days, 180))

    chunk = _symbol_chunk(symbol_id)
    if chunk is None:
        return {"error": f"unknown symbol_id: {symbol_id} — call search_symbols first for the exact id"}

    direct_files = {chunk.file_path}
    neighbor_files: set[str] = set()
    if include_blast_radius:
        neighborhood = graph_index.get_blast_radius(symbol_id)
        for nid in list(neighborhood.upstream) + list(neighborhood.downstream) + list(neighborhood.used_by) + list(neighborhood.uses):
            nchunk = _symbol_chunk(nid)
            if nchunk and nchunk.file_path not in direct_files:
                neighbor_files.add(nchunk.file_path)

    try:
        from app.rag.ingestion.git_ingestor import get_recent_history
        history = get_recent_history(days)
    except Exception as exc:
        return {"error": f"git history unavailable: {exc}"}

    direct_commits: list[dict[str, object]] = []
    nearby_commits: list[dict[str, object]] = []
    seen: set[str] = set()
    for commit in history:
        files = set(commit.get("files") or [])
        hit_direct = files & direct_files
        hit_nearby = files & neighbor_files
        if not (hit_direct or hit_nearby) or commit["hash"] in seen:
            continue
        seen.add(commit["hash"])
        entry = {
            "hash": commit["hash"][:12],
            "date": str(commit["date"])[:10],
            "author": commit["author"],
            "subject": commit["subject"],
        }
        if hit_direct:
            entry["touched"] = sorted(hit_direct)
            direct_commits.append(entry)
        else:
            entry["touched"] = sorted(hit_nearby)
            nearby_commits.append(entry)

    direct_commits.sort(key=lambda c: c["date"], reverse=True)
    nearby_commits.sort(key=lambda c: c["date"], reverse=True)
    return {
        "symbol_id": symbol_id,
        "file_path": chunk.file_path,
        "line_range": [chunk.start_line, chunk.end_line],
        "window_days": days,
        "direct_commits": direct_commits[:25],
        "direct_commit_count": len(direct_commits),
        "blast_radius_commits": nearby_commits[:25],
        "blast_radius_commit_count": len(nearby_commits),
        "blast_radius_files_checked": len(neighbor_files),
        "note": (
            "recent activity on this symbol's file — coordinate before changing"
            if direct_commits
            else "no direct commits in window; blast-radius activity only"
        ),
    }


def find_hotspots(days: int = 30, limit: int = 15, min_churn: int = 3, module_prefix: str = "") -> dict[str, object]:
    """Rank files by combined risk: git churn (commits in last N days) x graph connectivity x missing test coverage. High churn + highly connected + untested = change hotspot — where regressions are most likely and review effort is best spent. Use this to prioritize review and test-writing. Read-only, safe to call anytime."""
    limit = max(1, min(limit, 50))
    days = max(1, min(days, 365))

    try:
        from app.rag.ingestion.git_ingestor import get_file_churn
        churn = get_file_churn(days)
    except Exception as exc:
        return {"error": f"git history unavailable: {exc}"}

    from app.core.index_store import index_metadata_store
    snapshot = index_metadata_store.load_snapshot()
    if not snapshot:
        return {"error": "index snapshot unavailable — trigger a reindex"}

    file_symbols: dict[str, list] = {}
    for c in snapshot.chunks:
        if not c.file_path or c.is_test or _is_noise_symbol(c.symbol_id):
            continue
        if c.kind not in ("fn", "impl"):
            continue
        if module_prefix and not c.symbol_id.startswith(module_prefix):
            continue
        file_symbols.setdefault(c.file_path, []).append(c)

    degrees = graph_index.get_symbol_degrees()
    test_counts = graph_index.get_test_caller_counts()

    hotspots: list[dict[str, object]] = []
    for file_path, commit_count in churn.items():
        if commit_count < min_churn:
            continue
        symbols = file_symbols.get(file_path)
        if not symbols:
            continue
        max_sym = max(symbols, key=lambda c: degrees.get(c.symbol_id, 0))
        max_degree = degrees.get(max_sym.symbol_id, 0)
        file_degree = sum(degrees.get(c.symbol_id, 0) for c in symbols)
        anchor_tests = test_counts.get(max_sym.symbol_id, 0)
        untested_syms = sum(1 for c in symbols if test_counts.get(c.symbol_id, 0) == 0)
        untested_ratio = untested_syms / len(symbols)
        degree_factor = min(max_degree, 100) / 10
        test_factor = 1.5 if anchor_tests == 0 else (1.25 if untested_ratio > 0.8 else 1.0)
        score = commit_count * max(degree_factor, 1.0) * test_factor
        hotspots.append({
            "file_path": file_path,
            "score": round(score, 1),
            "components": {
                "commits": commit_count,
                "top_symbol_connections": max_degree,
                "file_total_connections": file_degree,
                "untested_symbol_ratio": round(untested_ratio, 2),
            },
            "anchor_symbol": {
                "symbol_id": max_sym.symbol_id,
                "connections": max_degree,
                "test_callers": anchor_tests,
                "line_range": [max_sym.start_line, max_sym.end_line],
            },
            "symbol_count": len(symbols),
        })

    hotspots.sort(key=lambda h: h["score"], reverse=True)
    return {
        "window_days": days,
        "min_churn": min_churn,
        "hotspot_count": len(hotspots),
        "hotspots": hotspots[:limit],
        "method": "score = commits x min(top-symbol connections,100)/10 x test penalty (1.5 untested anchor / 1.25 mostly untested / 1.0 covered)",
    }


def find_cycles(min_size: int = 2, limit: int = 10) -> dict[str, object]:
    """Find dependency cycles between modules — strongly connected components in the module-level call/type graph. Circular dependencies make changes ripple unpredictably and block clean testing; use this to find refactoring targets. Read-only, safe to call anytime."""
    limit = max(1, min(limit, 30))

    edges = graph_index.get_module_edges()
    if not edges:
        return {"cycles": [], "cycle_count": 0, "note": "no cross-module edges in the graph"}

    # Tarjan strongly connected components, iterative (no recursion limits)
    graph: dict[str, list[str]] = {}
    for src, dst in edges:
        graph.setdefault(src, []).append(dst)

    index_counter = [0]
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    sccs: list[list[str]] = []

    for root in graph:
        if root in index:
            continue
        work = [(root, iter(graph[root]))]
        while work:
            node, it = work[-1]
            if node not in index:
                index[node] = lowlink[node] = index_counter[0]
                index_counter[0] += 1
                stack.append(node)
                on_stack.add(node)
            advanced = False
            for succ in it:
                if succ not in graph:
                    continue
                if succ not in index:
                    work.append((succ, iter(graph[succ])))
                    advanced = True
                    break
                if succ in on_stack:
                    lowlink[node] = min(lowlink[node], index[succ])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index[node]:
                component: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    component.append(w)
                    if w == node:
                        break
                if len(component) >= max(min_size, 2):
                    sccs.append(sorted(component))

    def cycle_score(comp: list[str]) -> int:
        comp_set = set(comp)
        return sum(1 for s, d in edges if s in comp_set and d in comp_set)

    sccs.sort(key=lambda c: (-cycle_score(c), c))
    cycles = [
        {
            "modules": comp,
            "module_count": len(comp),
            "internal_edges": cycle_score(comp),
        }
        for comp in sccs[:limit]
    ]
    return {
        "cycle_count": len(sccs),
        "cycles": cycles,
        "note": (
            f"{len(sccs)} strongly connected module group(s); largest has "
            f"{max((len(c) for c in sccs), default=0)} modules"
        ) if sccs else "no module-level cycles found — the dependency graph is acyclic at module level",
    }


# ============================================================================
# Idea 2: Enhanced search_symbols with alias map + file path + fuzzy matching
# ============================================================================

def search_symbols_enhanced(query: str, limit: int = 20, search_files: bool = True, fuzzy: bool = True) -> dict[str, object]:
    """Enhanced symbol search with file path matching and fuzzy name resolution. Use this when search_symbols returns 0 matches — it searches file paths, short names, and partial matches. Provide a partial name or file path (e.g. 'deactivate_customer', 'dormancy_report.rs', 'customer_controller')."""
    limit = max(1, min(limit, 50))
    results = _filter_noise(graph_index.search_symbols(query, limit=max(limit * 2, 50)))[:limit]

    # Multi-word queries ("notification sms") match nothing as a single
    # substring — try each term and intersect/rank instead.
    terms = [t for t in query.lower().split() if len(t) >= 2]
    if not results and len(terms) > 1:
        try:
            from app.core.index_store import index_metadata_store
            snapshot = index_metadata_store.load_snapshot()
            if snapshot:
                term_sets: list[set[str]] = []
                for term in terms:
                    term_hits = {
                        c.symbol_id for c in snapshot.chunks
                        if term in c.symbol_id.lower()
                    }
                    if term_hits:
                        term_sets.append(term_hits)
                if term_sets:
                    from functools import reduce
                    matches = sorted(reduce(set.intersection, term_sets))[:limit]
                    results = [
                        {"symbol_id": sid, "short_name": sid.split("::")[-1],
                         "match_type": "multi_term_intersection"}
                        for sid in matches
                    ]
        except Exception:
            pass

    if not results and fuzzy:
        try:
            from app.core.index_store import index_metadata_store
            snapshot = index_metadata_store.load_snapshot()
            if snapshot:
                query_lower = query.lower()
                seen = set()
                for chunk in snapshot.chunks:
                    sid = chunk.symbol_id
                    if sid in seen:
                        continue
                    short_name = sid.split("::")[-1].lower()
                    if query_lower in short_name or short_name in query_lower:
                        seen.add(sid)
                        results.append({
                            "symbol_id": sid,
                            "short_name": sid.split("::")[-1],
                            "has_calls": False,
                            "has_uses": False,
                            "has_callers": False,
                            "has_users": False,
                            "match_type": "fuzzy_name",
                        })
                        if len(results) >= limit:
                            break
        except Exception:
            pass

    if search_files:
        try:
            from app.core.index_store import index_metadata_store
            snapshot = index_metadata_store.load_snapshot()
            if snapshot:
                query_lower = query.lower().replace(".rs", "")
                existing_sids = {r["symbol_id"] for r in results}
                for chunk in snapshot.chunks:
                    if chunk.symbol_id in existing_sids:
                        continue
                    if query_lower in chunk.file_path.lower():
                        results.append({
                            "symbol_id": chunk.symbol_id,
                            "short_name": chunk.symbol_id.split("::")[-1],
                            "file_path": chunk.file_path,
                            "has_calls": False,
                            "has_uses": False,
                            "has_callers": False,
                            "has_users": False,
                            "match_type": "file_path",
                        })
                        existing_sids.add(chunk.symbol_id)
                        if len(results) >= limit:
                            break
        except Exception:
            pass

    return {
        "query": query,
        "matches": len(results),
        "symbols": results,
    }


# ============================================================================
# Idea 4: Semantic search with timeout + fallback
# ============================================================================

def semantic_search(query: str, limit: int = 10) -> dict[str, object]:
    import time as _t

    from app.core.prom_metrics import SEMANTIC_SEARCH_LATENCY_MS

    _search_start = _t.monotonic()
    """Search the codebase by meaning, not just by name. Use this to find all code related to a concept (e.g. 'payment validation', 'wallet closure logic', 'authentication flow'). Returns matching symbols with relevance scores. Includes hybrid BM25+vector search, timeout protection, and automatic fallback to graph search if Weaviate is slow. After reviewing the results, call submit_search_feedback to indicate which results were helpful."""
    limit = max(1, min(limit, 25))

    try:
        from app.rag.reinforcement import feedback_store
        global_boosts = feedback_store.get_boost_weights()
        expansions = feedback_store.get_query_expansions(query)
        # Query-conditioned boosts: symbols helpful for SIMILAR past queries
        # replace the global weights — a payment-helpful symbol must not be
        # boosted for a dormancy query. Global weights are the cold-start
        # fallback for query shapes never seen before.
        similar_boosts = feedback_store.get_similar_query_boosts(query, limit=5)
        if similar_boosts:
            boost_weights = similar_boosts
        else:
            boost_weights = global_boosts
    except Exception:
        boost_weights = {}
        expansions = []

    expanded_query = query
    if expansions:
        expanded_query = query + " " + " ".join(term for term, _ in expansions)

    timed_out = False
    matches = []

    import concurrent.futures
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(semantic_index.query_chunks, expanded_query, limit * 2)
            matches = future.result(timeout=30)
    except concurrent.futures.TimeoutError:
        timed_out = True
        logger.warning("Semantic search timed out for query: %s — falling back to graph search", query[:60])
    except Exception:
        timed_out = True
        logger.warning("Semantic search failed for query: %s — falling back to graph search", query[:60])

    if timed_out or not matches:
        graph_results = graph_index.search_symbols(query, limit=limit)
        return _attach_stale_warning({
            "query": query,
            "query_id": uuid.uuid4().hex[:12],
            "matches": len(graph_results),
            "timed_out": timed_out,
            "fallback_used": "graph_search",
            "results": [
                {
                    "symbol_id": r["symbol_id"],
                    "score": 0.0,
                    "match_type": "graph_fallback",
                    "file_path": r.get("file_path", ""),
                }
                for r in graph_results
            ],
        })

    reranked = []
    for m in matches:
        if _is_noise_symbol(m.symbol_id):
            continue  # pseudo-symbols are not rankable code — never surface them
        boost = boost_weights.get(m.symbol_id, 0.0)
        adjusted = m.score + _BOOST_ALPHA * boost
        reranked.append((m, adjusted, boost))

    reranked.sort(key=lambda x: x[1], reverse=True)
    reranked = reranked[:limit]

    query_id = uuid.uuid4().hex[:12]

    # Implicit-feedback mining: remember which symbols this search served.
    # A later get_symbol_content on one of them is a click-through — the
    # agent voted with its attention, no explicit feedback needed.
    try:
        _RECENT_SEARCHES[query_id] = {
            "query": query,
            "at": time.monotonic(),
            "results": [m.symbol_id for m, _, _ in reranked],
        }
        if len(_RECENT_SEARCHES) > 200:
            cutoff = time.monotonic() - 600
            stale = [k for k, v in _RECENT_SEARCHES.items() if v["at"] < cutoff]
            for k in stale:
                _RECENT_SEARCHES.pop(k, None)
    except Exception:
        pass

    try:
        from app.rag.reinforcement.feedback_store import record_query_pattern
        record_query_pattern(query, [m.symbol_id for m, _, _ in reranked if boost > 0])
    except Exception:
        pass

    SEMANTIC_SEARCH_LATENCY_MS.observe((_t.monotonic() - _search_start) * 1000)
    return _attach_stale_warning({
        "query": query,
        "query_id": query_id,
        "matches": len(reranked),
        "expansion_applied": bool(expansions),
        "timed_out": False,
        "results": [
            {
                "symbol_id": m.symbol_id,
                "score": round(adj, 4),
                "original_score": round(m.score, 4),
                "boost": round(boost, 4),
                "file_path": m.source,
                "content_preview": m.content[:300],
            }
            for m, adj, boost in reranked
        ],
    })


# ============================================================================
# Idea 6: MCP tool chaining — analyze_pr_diff
# ============================================================================

def analyze_pr_diff(diff_text: str, max_symbols: int = 10) -> dict[str, object]:
    """Analyze a git diff and return changed symbols with blast radius in one call. This is a chained tool that: (1) parses the diff for function/struct/trait names, (2) resolves them against the graph, (3) gets blast radius for each resolved symbol, (4) returns a combined impact report. Use this instead of calling search_symbols + get_blast_radius separately. Provide the full git diff text."""
    from app.rag.diff_parser import resolve_diff_symbols

    resolved = resolve_diff_symbols(diff_text, graph_index)

    blast_radius_results: list[dict] = []
    for sym in resolved["changed_symbols"][:max_symbols]:
        sid = sym["symbol_id"]
        br = get_blast_radius(sid)
        if "error" not in br:
            blast_radius_results.append({
                "symbol_id": sid,
                "short_name": sym["short_name"],
                "status": sym["status"],
                "change_type": sym.get("change_type", "unknown"),
                "symbol_type": sym.get("symbol_type", "unknown"),
                "change_details": sym.get("change_details", ""),
                "risk_score": br.get("risk_score", "unknown"),
                "risk_factors": br.get("risk_factors", {}),
                "total_connections": br.get("total_connections", 0),
                "upstream_count": br.get("total_upstream", 0),
                "downstream_count": br.get("total_downstream", 0),
                "file_path": br.get("file_path", ""),
            })

    return _attach_stale_warning({
        "changed_files": resolved["changed_files"],
        "resolved_symbols": blast_radius_results,
        "new_symbols_not_in_index": resolved["new_symbols"],
        "deleted_symbols": resolved["deleted_symbols"],
        "summary": resolved["summary"],
        "high_risk_symbols": [r for r in blast_radius_results if r.get("risk_score") in ("high", "critical")],
        "change_type_breakdown": resolved.get("summary", {}).get("change_type_breakdown", {}),
    })


# ============================================================================
# Idea 7: Confidence scoring on blast radius edges
# ============================================================================

def get_blast_radius_detailed(symbol_id: str) -> dict[str, object]:
    """Get blast radius with confidence scores on each edge. Confidence: 'high' = direct caller/callee, 'medium' = type reference, 'low' = inferred. Use this when you need to know how reliable each connection is. Provide the full symbol_id."""
    br = get_blast_radius(symbol_id)
    if "error" in br:
        return br

    detailed = {
        "symbol_id": br["symbol_id"],
        "risk_score": br.get("risk_score"),
        "total_connections": br.get("total_connections", 0),
        "upstream": [],
        "downstream": [],
        "used_by": [],
        "uses": [],
    }

    used_by_modes = br.get("used_by_modes", {})
    uses_modes = br.get("uses_modes", {})

    for sym in br.get("upstream", []):
        detailed["upstream"].append({"symbol_id": sym, "confidence": "high", "edge_type": "calls"})
    for sym in br.get("downstream", []):
        detailed["downstream"].append({"symbol_id": sym, "confidence": "high", "edge_type": "calls"})
    for sym in br.get("used_by", []):
        modes = used_by_modes.get(sym, ["reference"])
        detailed["used_by"].append({"symbol_id": sym, "confidence": "medium", "edge_type": "type_reference", "usage_modes": modes})
    for sym in br.get("uses", []):
        modes = uses_modes.get(sym, ["reference"])
        detailed["uses"].append({"symbol_id": sym, "confidence": "medium", "edge_type": "type_reference", "usage_modes": modes})

    total_high = len(detailed["upstream"]) + len(detailed["downstream"])
    total_medium = len(detailed["used_by"]) + len(detailed["uses"])
    detailed["confidence_summary"] = {
        "high_confidence_edges": total_high,
        "medium_confidence_edges": total_medium,
        "overall_confidence": "high" if total_high > total_medium else "medium" if total_medium > 0 else "low",
    }

    meta = _get_symbol_metadata(symbol_id)
    if meta:
        detailed["kind"] = meta["kind"]
        detailed["file_path"] = meta["file_path"]
        detailed["start_line"] = meta["start_line"]
        detailed["end_line"] = meta["end_line"]

    return detailed


# ============================================================================
# Idea 8: Graph meta — build provenance for trust verification
# ============================================================================

_CLASSIFIER_VERSION = "3"

_USAGE_MODE_WEIGHTS = {
    "pattern_match": 3,
    "construction": 2,
    "field_declaration": 2,
    "trait_impl": 2,
    "type_reference": 1,
    "type_param": 1,
    "import": 0,
    "reference": 0,
}


def get_index_meta() -> dict[str, object]:
    """Get graph build metadata — build timestamp, commit hash, commit drift (commits_behind), coverage stats, gen number, and collection info. Use this to verify the graph is fresh and which classifier version was used. Read-only, safe to call anytime."""
    from app.core.index_store import index_metadata_store
    from app.rag.retrieval.graph import _get_current_gen, graph_index

    snapshot = index_metadata_store.load_snapshot()
    gen = _get_current_gen() if hasattr(graph_index._active(), "_gen") else 0

    try:
        from app.rag.ingestion.git_ingestor import get_last_indexed_commit, get_current_head
        last_commit = get_last_indexed_commit()
        try:
            head = get_current_head()
        except Exception:
            head = "unknown"
    except Exception:
        last_commit = ""
        head = "unknown"

    weaviate_collection = ""
    if hasattr(graph_index._active(), "_gen"):
        try:
            weaviate_collection = semantic_index.get_active_collection_name()
        except Exception:
            pass

    # Coverage: files on disk vs indexed (tests are excluded by design).
    coverage = None
    if snapshot and snapshot.repository_path:
        try:
            from pathlib import Path

            repo = Path(snapshot.repository_path)
            disk = [
                p for p in repo.rglob("*.rs")
                if ".git" not in p.parts and "target" not in p.parts
                and "tests" not in p.relative_to(repo).parts
                and not p.relative_to(repo).name.endswith(("_test.rs", "_tests.rs", "tests.rs", "test.rs"))
            ]
            indexed_files = {c.file_path for c in snapshot.chunks}
            missing = [str(p.relative_to(repo)) for p in disk if str(p.relative_to(repo)) not in indexed_files]
            coverage = {
                "rust_files_on_disk": len(disk),
                "files_indexed": len(indexed_files),
                "missing_from_index": missing[:20],
                "missing_count": len(missing),
            }
        except Exception:
            coverage = None

    # Commit drift: how many commits HEAD is ahead of the indexed commit.
    commits_behind = None
    if last_commit and head and head != "unknown" and last_commit != head:
        try:
            import subprocess as _sp

            out = _sp.run(
                ["git", "-C", snapshot.repository_path, "rev-list", "--count", f"{last_commit}..{head}"],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip().isdigit():
                commits_behind = int(out.stdout.strip())
        except Exception:
            pass

    return {
        "graph_gen": gen,
        "classifier_version": _CLASSIFIER_VERSION,
        "last_indexed_commit": last_commit[:12],
        "current_head": head[:12],
        "commits_behind": commits_behind,
        "up_to_date": last_commit == head if last_commit and head != "unknown" else False,
        "snapshot_created_at": snapshot.created_at.isoformat() if snapshot and snapshot.created_at else None,
        "files_indexed": snapshot.files_indexed if snapshot else 0,
        "total_symbols": len(snapshot.chunks) if snapshot else 0,
        "total_edges": len(snapshot.graph_edges) if snapshot else 0,
        "weaviate_collection": weaviate_collection,
        "embedding_model": "BAAI/bge-base-en-v1.5",
        "coverage": coverage,
        "embedding_dimensions": 768,
        "features": [
            "hybrid_bm25_vector_search",
            "chunk_enrichment",
            "usage_mode_classification",
            "risk_factors",
            "change_classification",
            "incremental_ingest",
        ],
    }


# ============================================================================
# Idea 9: File path → symbols inverse lookup
# ============================================================================

def get_symbols_in_file(file_path: str) -> dict[str, object]:
    """List all symbols defined in a given file. Use this to resolve a diff's file path to the exact symbols that changed — avoids guessing symbol names. Provide a file path like 'crates/common/src/redis/wrapper.rs'."""
    from app.core.index_store import index_metadata_store

    snapshot = index_metadata_store.load_snapshot()
    if snapshot is None:
        return {"error": "No index snapshot found.", "file_path": file_path}

    normalized = file_path.lstrip("/").strip()
    symbols: list[dict] = []
    for chunk in snapshot.chunks:
        if chunk.file_path == normalized or chunk.file_path.endswith(normalized) or normalized.endswith(chunk.file_path):
            symbols.append({
                "symbol_id": chunk.symbol_id,
                "kind": chunk.kind,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
            })

    symbols.sort(key=lambda s: s["start_line"])
    return {
        "file_path": file_path,
        "symbols_found": len(symbols),
        "symbols": symbols,
    }


# ============================================================================
# Idea 10: Decision model gateway — typed yes/no, score, choice judgments
# ============================================================================

_DECISION_LOCK = threading.Lock()


def resolve_stacktrace(stacktrace: str, include_neighborhood: bool = False) -> dict[str, object]:
    """Map panic backtraces or file:line log entries to graph symbols. Provide raw stacktrace text (any format) — every 'path/to/file.rs:LINE' found is resolved to the innermost enclosing symbol with its location. Use this during incidents to go from a crash to the code neighborhood in one call. Optional: include_neighborhood=true adds a compact blast-radius summary per frame."""
    import re as _re

    from app.core.index_store import index_metadata_store

    frames = _re.findall(r"([A-Za-z0-9_./-]+\.rs):(\d+)", stacktrace)
    if not frames:
        return {"error": "no 'file.rs:line' frames found in the stacktrace text"}

    snapshot = index_metadata_store.load_snapshot()
    if not snapshot:
        return {"error": "no index snapshot loaded"}

    by_file: dict[str, list] = {}
    for c in snapshot.chunks:
        by_file.setdefault(c.file_path, []).append(c)

    resolved: list[dict] = []
    seen: set[str] = set()
    for file_path, line_str in frames:
        line = int(line_str)
        candidates = [
            c for c in by_file.get(file_path, [])
            if c.start_line <= line <= c.end_line and c.kind in ("fn", "method", "struct", "enum", "trait", "impl", "module")
        ]
        if not candidates:
            # tolerate path prefix differences (leading crates/, src/)
            suffix = file_path.rsplit("/", 2)[-1] if "/" in file_path else file_path
            candidates = [
                c for c in snapshot.chunks
                if c.file_path.endswith(suffix) and c.start_line <= line <= c.end_line
            ]
        if not candidates:
            resolved.append({"file_path": file_path, "line": line, "symbol_id": None,
                             "note": "no enclosing symbol in the index"})
            continue
        innermost = max(candidates, key=lambda c: c.start_line)
        key = f"{innermost.symbol_id}:{line}"
        if key in seen:
            continue
        seen.add(key)
        entry = {
            "file_path": file_path,
            "line": line,
            "symbol_id": innermost.symbol_id,
            "kind": innermost.kind,
            "symbol_lines": [innermost.start_line, innermost.end_line],
        }
        if include_neighborhood and graph_index.has_symbol(innermost.symbol_id):
            br = get_blast_radius(innermost.symbol_id, summary_only=True)
            entry["callers"] = br.get("callers_direct_count", 0)
            entry["type_references"] = br.get("type_reference_count", 0)
            entry["risk_score"] = br.get("risk_score", "unknown")
        resolved.append(entry)

    return {
        "frames_found": len(frames),
        "resolved": len([r for r in resolved if r.get("symbol_id")]),
        "frames": resolved,
    }


def find_dead_code(limit: int = 50, module_prefix: str = "") -> dict[str, object]:
    """Find symbols with zero inbound callers or type-references in the index — cleanup candidates and a parser-accuracy check (false positives usually point to missed edges from dynamic dispatch, macros, or trait objects). Excludes test files, pseudo-symbols, and main/entry functions. Optional: module_prefix to scope, limit (default 50)."""
    limit = max(1, min(limit, 200))
    with graph_index._active()._get_driver().session() as session:
        result = session.run(
            """
            MATCH (s:Symbol {gen: $gen})
            WHERE (s.id STARTS WITH $prefix)
              AND NOT EXISTS { ()-[:CALLS]->(s) }
              AND NOT EXISTS { ()-[:USES]->(s) }
              AND NOT s.id ENDS WITH '::file_summary'
              AND NOT s.id ENDS WITH '::module_exports'
              AND NOT s.id ENDS WITH '::main'
              AND NOT s.id CONTAINS 'test'
              AND NOT s.id ENDS WITH '::fn'
            RETURN s.id AS symbol_id,
                   [(s)-[:CALLS|USES]->(t) | t.id][0..5] AS calls_out
            ORDER BY size(calls_out) DESC
            LIMIT $limit
            """,
            gen=graph_index._active()._gen,
            prefix=module_prefix,
            limit=limit,
        )
        dead = [
            {
                "symbol_id": r["symbol_id"],
                "outgoing_connections": len(r["calls_out"]),
                "caveat": "no inbound edges in the index — verify against dynamic dispatch / macro callers before deleting",
            }
            for r in result
        ]
    return {
        "dead_symbols": dead,
        "count": len(dead),
        "caveat": "the index has no visibility into dyn-dispatch, macro-generated calls, or external crate consumers — treat as cleanup candidates, not a deletion list",
    }


def diff_modules(module_a: str, module_b: str) -> dict[str, object]:
    """Diff the symbol sets of two modules — finds drift between counterparts (e.g. a sync report generator vs its async version, a handler and its mirror). Returns symbols present in A but missing from B and vice versa, matched by short name. Use this to catch sync/async implementations that have drifted apart (fields or functions added to one but not the other). Provide module prefixes, e.g. 'dashboard::product::mis_report' and 'dashboard::product::generators'."""
    try:
        from app.core.index_store import index_metadata_store
        snapshot = index_metadata_store.load_snapshot()
    except Exception:
        return {"error": "index metadata store unavailable"}
    if not snapshot:
        return {"error": "no snapshot loaded"}

    def _symbols_for(prefix: str) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        p = prefix.strip().strip(":")
        for c in snapshot.chunks:
            sid = c.symbol_id
            if sid == p or sid.startswith(p + "::"):
                if c.kind in ("file_summary", "module_exports"):
                    continue
                out.setdefault(sid.split("::")[-1], []).append(sid)
        return out

    a = _symbols_for(module_a)
    b = _symbols_for(module_b)
    if not a:
        return {"error": f"no symbols found under '{module_a}' — check the prefix with search_symbols"}
    if not b:
        return {"error": f"no symbols found under '{module_b}' — check the prefix with search_symbols"}

    only_a = sorted({sid for name, sids in a.items() if name not in b for sid in sids})
    only_b = sorted({sid for name, sids in b.items() if name not in a for sid in sids})
    shared = sorted(set(a) & set(b))

    return {
        "module_a": module_a,
        "module_b": module_b,
        "symbols_a": len(a),
        "symbols_b": len(b),
        "shared_symbol_names": len(shared),
        "only_in_a": only_a[:100],
        "only_in_a_count": len(only_a),
        "only_in_b": only_b[:100],
        "only_in_b_count": len(only_b),
        "drift_score": round(len(only_a) + len(only_b)) / max(1, len(shared) + len(only_a) + len(only_b)),
        "note": "only_in_a / only_in_b are the drift — counterparts missing from one side",
    }


def find_warnings_in_blast_radius(symbol_id: str, radius: int = 2, kinds: list[str] | None = None) -> dict[str, object]:
    """Find code warnings (TODO/FIXME/HACK comments, swallowed errors, stub functions, commented-out code, fail-open defaults) in a symbol's blast radius. This is the metal detector: bugs live in comments, dead code, and discarded Results — none visible in the dependency graph. Use during PR review or incident triage on hotspots. Provide the full symbol_id. Optional: radius (default 2, max 3 — how many dependency hops to include), kinds=['todo','fixme','hack','xxx','commented_code','error_swallow','fail_open','stub_fn','unsafe_block','ffi_boundary','panic_path','unwrap_density'] to filter."""
    if not graph_index.has_symbol(symbol_id):
        return {"error": f"Symbol '{symbol_id}' not found in the graph index.", "symbol_id": symbol_id, "hint": "Use search_symbols to find the correct symbol_id."}
    radius = max(1, min(int(radius), 3))

    # Collect the neighborhood up to `radius` hops, both directions.
    frontier = {symbol_id}
    seen = {symbol_id}
    for _ in range(radius):
        next_frontier: set[str] = set()
        for sym in frontier:
            neighborhood = (
                graph_index.get_blast_radius(sym)
                if hasattr(graph_index, "get_blast_radius")
                else graph_index.get_neighbors(sym)
            )
            result = neighborhood.model_dump() if hasattr(neighborhood, "model_dump") else neighborhood
            for group in ("upstream", "downstream", "used_by", "uses"):
                for entry in result.get(group, []):
                    target = entry.get("symbol_id") if isinstance(entry, dict) else entry
                    if target and target not in seen:
                        seen.add(target)
                        next_frontier.add(target)
        frontier = next_frontier
        if not frontier:
            break

    try:
        from app.rag.ingestion import observation_store
        observations = observation_store.get_observations_for_symbols(sorted(seen), kinds=kinds)
    except Exception:
        return {"error": "observation store unavailable", "symbol_id": symbol_id, "hint": "Postgres may be down"}

    by_symbol: dict[str, list[dict]] = {}
    for o in observations:
        by_symbol.setdefault(o["symbol_id"], []).append(
            {"kind": o["kind"], "file_path": o["file_path"], "line": o["line"], "detail": o["detail"]}
        )
    kind_counts: dict[str, int] = {}
    for o in observations:
        kind_counts[o["kind"]] = kind_counts.get(o["kind"], 0) + 1

    return {
        "symbol_id": symbol_id,
        "radius": radius,
        "symbols_scanned": len(seen),
        "warnings_total": len(observations),
        "warnings_by_kind": kind_counts,
        "warnings": by_symbol,
    }


def make_decision(state: str, questions: dict) -> dict[str, object]:
    """Get fast, typed judgments from a local decision model (Jev-style System One). Sends your state text plus named questions and gets back a choice, a score, or a calibrated yes/no probability (noul) for each — NOT chat. Takes 10-35 seconds per call (local CPU model), so use it for decisions worth waiting on: PR risk assessment, triage routing, content gating — not for anything per-message. Provide 'state' (the text/JSON to judge, max 2000 chars). All questions are answered in ONE model pass (~10-60s total, not per question) — set client timeouts to at least 120s and 'questions': an object of up to 8 named questions, each {type: 'noul'|'score'|'choice', instructions: string, criteria: for choice — an object of allowed values; for score — an array of labels low to high}. Example: {"state": "PR changes 446 files in wallet closure", "questions": {"risk": {"type": "score", "instructions": "How risky?", "criteria": ["low", "medium", "high"]}, "needs_review": {"type": "noul", "instructions": "Needs senior review?"}}}"""
    import json as _json

    from app.core import systemone

    # Tolerate clients that stringify nested args (common with JSON-RPC).
    if isinstance(questions, str):
        try:
            questions = _json.loads(questions)
        except _json.JSONDecodeError:
            return {"error": "questions: received a string that is not valid JSON — send an object of named questions"}
    if not isinstance(state, str):
        if isinstance(state, dict):
            state = _json.dumps(state)
        else:
            state = str(state)
    if not state.strip():
        return {"error": "state must be a non-empty string"}
    if len(state) > systemone.MAX_STATE_CHARS:
        return {
            "error": f"state too long ({len(state)} chars, max {systemone.MAX_STATE_CHARS}) — truncate or summarize the state",
        }
    if not isinstance(questions, dict) or not questions:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "make_decision rejected questions: type=%s len=%s sample=%.80r",
            type(questions).__name__,
            len(questions) if hasattr(questions, "__len__") else "n/a",
            questions,
        )
        return {"error": "questions must be a non-empty object of named questions"}
    if len(questions) > systemone.MAX_QUESTIONS:
        return {"error": f"too many questions ({len(questions)}, max {systemone.MAX_QUESTIONS})"}
    for name, q in questions.items():
        if not isinstance(q, dict) or q.get("type") not in systemone._ALLOWED_TYPES:
            return {"error": f"question '{name}' must have type noul, score, or choice"}

    if not _DECISION_LOCK.acquire(blocking=False):
        return {
            "error": "decision model busy with another request — it processes one at a time (~30s); retry shortly"
        }
    try:
        answers = systemone.decide(state, questions)
        if isinstance(answers, dict) and answers.get("__busy__"):
            return {
                "error": "decision model busy with background feedback gating — it processes one request at a time; retry in ~30s",
            }
        if answers is None:
            return {
                "error": "decision model unavailable (service down or timed out)",
                "hint": "try again later or decide without the model",
            }
        return {"answers": answers, "model": "nimble"}
    finally:
        _DECISION_LOCK.release()
