"""Read-only graph MCP tools for opencode agent consumption.

These tools expose ONLY graph read operations and symbol content.
No write, index, file, cargo, or mutation operations are accessible.
"""

import logging
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


def search_symbols(query: str, limit: int = 20) -> dict[str, object]:
    """Search for symbols in the graph by partial name match. Use this FIRST when you don't know the exact symbol_id — it returns matching symbol IDs that you can then pass to get_blast_radius or traverse_graph. Provide a partial name (e.g. 'InventoryClient', 'process_review', 'tag_inventory')."""
    limit = max(1, min(limit, 50))
    results = graph_index.search_symbols(query, limit=limit)
    return {
        "query": query,
        "matches": len(results),
        "symbols": results,
    }


def get_blast_radius(symbol_id: str, usage_modes_filter: list[str] | None = None) -> dict[str, object]:
    """Get the immediate blast radius of a symbol — its direct callers, callees, type-users, and used types. Use this during PR review to understand the impact of changing a symbol. Provide the full symbol_id (e.g. 'crates::inventory::client::InventoryClient'). Optional: pass usage_modes_filter=['pattern_match', 'construction'] to return only callers that pattern-match or construct this type."""
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

    result["total_upstream"] = len(upstream)
    result["total_downstream"] = len(downstream)
    result["total_used_by"] = len(used_by)
    result["total_uses"] = len(uses)
    result["total_connections"] = len(upstream) + len(downstream) + len(used_by) + len(uses)
    result["risk_score"] = _compute_risk_score(upstream, downstream, used_by, uses)
    result["risk_factors"] = _compute_risk_factors(upstream, downstream, used_by, uses)

    meta = _get_symbol_metadata(symbol_id)
    if meta:
        result["kind"] = meta["kind"]
        result["file_path"] = meta["file_path"]
        result["start_line"] = meta["start_line"]
        result["end_line"] = meta["end_line"]

    result["upstream"] = upstream[:_MAX_PER_DIRECTION]
    result["downstream"] = downstream[:_MAX_PER_DIRECTION]
    result["used_by"] = used_by[:_MAX_PER_DIRECTION]
    result["uses"] = uses[:_MAX_PER_DIRECTION]
    result["used_by_modes"] = result.get("used_by_modes", {})
    result["uses_modes"] = result.get("uses_modes", {})

    if usage_modes_filter:
        used_by_modes = result.get("used_by_modes", {})
        uses_modes = result.get("uses_modes", {})
        result["used_by"] = [s for s in result["used_by"] if any(m in used_by_modes.get(s, ["reference"]) for m in usage_modes_filter)]
        result["uses"] = [s for s in result["uses"] if any(m in uses_modes.get(s, ["reference"]) for m in usage_modes_filter)]
        result["filtered_by_usage_modes"] = usage_modes_filter

    usage_modes: dict[str, int] = {}
    for mode_list in list(result.get("used_by_modes", {}).values()) + list(result.get("uses_modes", {}).values()):
        for mode in mode_list:
            usage_modes[mode] = usage_modes.get(mode, 0) + 1
    result["usage_mode_summary"] = usage_modes

    return result


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
                if isinstance(edge, dict):
                    edge_sid = edge["symbol_id"]
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


def get_symbol_content(symbol_id: str) -> dict[str, object]:
    """Get the source code content of a symbol — its full implementation, file path, and line range. Use this after get_blast_radius to understand what a symbol actually does. Provide the full symbol_id."""
    meta = _get_symbol_metadata(symbol_id)
    if not meta:
        return {"error": f"Symbol '{symbol_id}' not found in the index.", "symbol_id": symbol_id, "hint": "Use search_symbols to find the correct symbol_id."}

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
                    "content": chunk.content[:8000],
                    "truncated": len(chunk.content) > 8000,
                }

    return {"error": f"Symbol '{symbol_id}' found in metadata but content not available.", "symbol_id": symbol_id}


def semantic_search(query: str, limit: int = 10) -> dict[str, object]:
    """Search the codebase by meaning, not just by name. Use this to find all code related to a concept (e.g. 'payment validation', 'wallet closure logic', 'authentication flow'). Returns matching symbols with relevance scores. After reviewing the results, call submit_search_feedback to indicate which results were helpful — the system learns from feedback and improves future searches. Provide a natural language query."""
    limit = max(1, min(limit, 25))

    try:
        from app.rag.reinforcement import feedback_store
        boost_weights = feedback_store.get_boost_weights()
        expansions = feedback_store.get_query_expansions(query)
    except Exception:
        boost_weights = {}
        expansions = []

    if expansions:
        expanded_query = query + " " + " ".join(term for term, _ in expansions)
    else:
        expanded_query = query

    matches = semantic_index.query_chunks(expanded_query, limit=limit * 2)

    reranked = []
    for m in matches:
        boost = boost_weights.get(m.symbol_id, 0.0)
        adjusted = m.score + _BOOST_ALPHA * boost
        reranked.append((m, adjusted, boost))

    reranked.sort(key=lambda x: x[1], reverse=True)
    reranked = reranked[:limit]

    query_id = uuid.uuid4().hex[:12]

    return {
        "query": query,
        "query_id": query_id,
        "matches": len(reranked),
        "expansion_applied": bool(expansions),
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
    }


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
        result = session.run(
            f"""
            MATCH path = shortestPath(
                (start:Symbol {{id: $from_id, gen: $gen}})-[:CALLS*1..{max_depth}]-(end:Symbol {{id: $to_id, gen: $gen}})
            )
            RETURN [node in nodes(path) | node.id] AS symbol_path,
                   [rel in relationships(path) | type(rel)] AS edge_types
            """,
            from_id=from_symbol,
            to_id=to_symbol,
            gen=gen,
        )
        record = result.single()

        if not record:
            result = session.run(
                f"""
                MATCH path = shortestPath(
                    (start:Symbol {{id: $from_id, gen: $gen}})-[:CALLS|USES*1..{max_depth}]-(end:Symbol {{id: $to_id, gen: $gen}})
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

    if not record:
        return {"from": from_symbol, "to": to_symbol, "path_found": False, "message": f"No path found within {max_depth} hops."}

    symbol_path = record["symbol_path"]
    edge_types = record["edge_types"]

    return {
        "from": from_symbol,
        "to": to_symbol,
        "path_found": True,
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

    neighborhoods = graph_index.traverse(symbol_id, depth=depth)

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
    truncated = total_neighborhoods > _MAX_TRAVERSE_NEIGHBORHOODS

    if summary_only:
        hop_counts: list[dict] = []
        for i, n in enumerate(neighborhoods):
            nb = n.model_dump() if hasattr(n, "model_dump") else n
            hop_counts.append({
                "hop": i,
                "symbol_id": nb.get("symbol_id", ""),
                "upstream_count": len(nb.get("upstream", [])),
                "downstream_count": len(nb.get("downstream", [])),
                "used_by_count": len(nb.get("used_by", [])),
                "uses_count": len(nb.get("uses", [])),
            })
        return {
            "root_symbol": symbol_id,
            "depth": depth,
            "symbols_visited": total_neighborhoods,
            "total_reachable_symbols": len(all_symbols),
            "truncated": truncated,
            "summary": hop_counts,
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
    """Get current graph index statistics — total nodes and edges. Read-only, safe to call anytime."""
    return graph_index.get_stats()


# ============================================================================
# Idea 2: Enhanced search_symbols with alias map + file path + fuzzy matching
# ============================================================================

def search_symbols_enhanced(query: str, limit: int = 20, search_files: bool = True, fuzzy: bool = True) -> dict[str, object]:
    """Enhanced symbol search with file path matching and fuzzy name resolution. Use this when search_symbols returns 0 matches — it searches file paths, short names, and partial matches. Provide a partial name or file path (e.g. 'deactivate_customer', 'dormancy_report.rs', 'customer_controller')."""
    limit = max(1, min(limit, 50))
    results = graph_index.search_symbols(query, limit=limit)

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
    """Search the codebase by meaning, not just by name. Use this to find all code related to a concept (e.g. 'payment validation', 'wallet closure logic', 'authentication flow'). Returns matching symbols with relevance scores. Includes hybrid BM25+vector search, timeout protection, and automatic fallback to graph search if Weaviate is slow. After reviewing the results, call submit_search_feedback to indicate which results were helpful."""
    limit = max(1, min(limit, 25))

    try:
        from app.rag.reinforcement import feedback_store
        boost_weights = feedback_store.get_boost_weights()
        expansions = feedback_store.get_query_expansions(query)
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
        return {
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
        }

    reranked = []
    for m in matches:
        boost = boost_weights.get(m.symbol_id, 0.0)
        adjusted = m.score + _BOOST_ALPHA * boost
        reranked.append((m, adjusted, boost))

    reranked.sort(key=lambda x: x[1], reverse=True)
    reranked = reranked[:limit]

    query_id = uuid.uuid4().hex[:12]

    try:
        from app.rag.reinforcement.feedback_store import record_query_pattern
        record_query_pattern(query, [m.symbol_id for m, _, _ in reranked if boost > 0])
    except Exception:
        pass

    return {
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
    }


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

    return {
        "changed_files": resolved["changed_files"],
        "resolved_symbols": blast_radius_results,
        "new_symbols_not_in_index": resolved["new_symbols"],
        "deleted_symbols": resolved["deleted_symbols"],
        "summary": resolved["summary"],
        "high_risk_symbols": [r for r in blast_radius_results if r.get("risk_score") in ("high", "critical")],
        "change_type_breakdown": resolved.get("summary", {}).get("change_type_breakdown", {}),
    }


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
    """Get graph build metadata — build timestamp, commit hash, classifier version, gen number, and collection info. Use this to verify the graph is fresh and which classifier version was used. Read-only, safe to call anytime."""
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

    return {
        "graph_gen": gen,
        "classifier_version": _CLASSIFIER_VERSION,
        "last_indexed_commit": last_commit[:12],
        "current_head": head[:12],
        "up_to_date": last_commit == head if last_commit and head != "unknown" else False,
        "snapshot_created_at": snapshot.created_at.isoformat() if snapshot and snapshot.created_at else None,
        "files_indexed": snapshot.files_indexed if snapshot else 0,
        "total_symbols": len(snapshot.chunks) if snapshot else 0,
        "total_edges": len(snapshot.graph_edges) if snapshot else 0,
        "weaviate_collection": weaviate_collection,
        "embedding_model": "BAAI/bge-base-en-v1.5",
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
