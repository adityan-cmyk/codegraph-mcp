"""Stateless read-only MCP server for opencode agent consumption.

Implements the MCP JSON-RPC protocol over HTTP without session management.
Each request is self-contained — no session IDs are generated or tracked.
This means server restarts don't break connected clients.

Only graph read operations (blast radius + traversal) are exposed.
No write, index, file, cargo, or mutation tools are available.
"""

import asyncio
import inspect
import json
import logging
import traceback
import uuid
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from uvicorn import Config, Server

from app.mcp.tools.graph_read_tools import (
    get_blast_radius,
    traverse_graph,
    get_graph_stats,
    search_symbols,
    batch_blast_radius,
    get_symbol_content,
    semantic_search,
    find_dependency_path,
    submit_search_feedback,
    get_reinforcement_stats,
    submit_ai_feedback,
    search_symbols_enhanced,
    analyze_pr_diff,
    get_blast_radius_detailed,
    get_index_meta,
    get_symbols_in_file,
)

logger = logging.getLogger(__name__)

_SERVER_INFO = {
    "name": "oncall-graph",
    "version": "2.0.0",
}

_INSTRUCTIONS = "Read-only code graph: blast-radius, traversal, semantic search, feedback."

_PROTOCOL_VERSION = "2024-11-05"

_CAPABILITIES = {"tools": {"listChanged": True}}

_TOOLS: dict[str, Any] = {
    "get_blast_radius": {
        "handler": get_blast_radius,
        "description": inspect.getdoc(get_blast_radius) or "",
        "schema": {
            "type": "object",
            "properties": {
                "symbol_id": {
                    "type": "string",
                    "description": "Full symbol path, e.g. 'crates::inventory::client::InventoryClient'",
                },
                "usage_modes_filter": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["pattern_match", "construction", "field_declaration", "trait_impl", "type_param", "import", "type_reference"]},
                    "description": "Filter used_by/uses edges to only those with matching usage modes. E.g. ['pattern_match', 'construction'] returns only callers that pattern-match or construct this type.",
                    "default": [],
                },
            },
            "required": ["symbol_id"],
        },
    },
    "traverse_graph": {
        "handler": traverse_graph,
        "description": inspect.getdoc(traverse_graph) or "",
        "schema": {
            "type": "object",
            "properties": {
                "symbol_id": {
                    "type": "string",
                    "description": "Full symbol path",
                },
                "depth": {
                    "type": "integer",
                    "description": "How many hops to traverse (default 1, max 5)",
                    "default": 1,
                    "minimum": 1,
                    "maximum": 5,
                },
                "summary_only": {
                    "type": "boolean",
                    "description": "Return only counts per hop, not full neighborhood data. Use when you only need blast radius size.",
                    "default": False,
                },
            },
            "required": ["symbol_id"],
        },
    },
    "get_graph_stats": {
        "handler": get_graph_stats,
        "description": inspect.getdoc(get_graph_stats) or "",
        "schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "search_symbols": {
        "handler": search_symbols,
        "description": inspect.getdoc(search_symbols) or "",
        "schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Partial symbol name to search for (e.g. 'InventoryClient', 'process_review')",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 20, max 50)",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 50,
                },
            },
            "required": ["query"],
        },
    },
    "batch_blast_radius": {
        "handler": batch_blast_radius,
        "description": inspect.getdoc(batch_blast_radius) or "",
        "schema": {
            "type": "object",
            "properties": {
                "symbol_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of full symbol_ids to query",
                },
            },
            "required": ["symbol_ids"],
        },
    },
    "get_symbol_content": {
        "handler": get_symbol_content,
        "description": inspect.getdoc(get_symbol_content) or "",
        "schema": {
            "type": "object",
            "properties": {
                "symbol_id": {
                    "type": "string",
                    "description": "Full symbol path",
                },
            },
            "required": ["symbol_id"],
        },
    },
    "semantic_search": {
        "handler": semantic_search,
        "description": inspect.getdoc(semantic_search) or "",
        "schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language query (e.g. 'payment validation', 'wallet closure')",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 10, max 25)",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 25,
                },
            },
            "required": ["query"],
        },
    },
    "find_dependency_path": {
        "handler": find_dependency_path,
        "description": inspect.getdoc(find_dependency_path) or "",
        "schema": {
            "type": "object",
            "properties": {
                "from_symbol": {
                    "type": "string",
                    "description": "Starting symbol_id",
                },
                "to_symbol": {
                    "type": "string",
                    "description": "Target symbol_id",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Max hops to search (default 5, max 10)",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["from_symbol", "to_symbol"],
        },
    },
    "submit_search_feedback": {
        "handler": submit_search_feedback,
        "description": inspect.getdoc(submit_search_feedback) or "",
        "schema": {
            "type": "object",
            "properties": {
                "query_text": {
                    "type": "string",
                    "description": "The original search query that was used (required)",
                },
                "symbol_id": {
                    "type": "string",
                    "description": "The symbol_id from the search result being rated. Omit for query-level feedback (entire result set was poor).",
                    "default": "",
                },
                "feedback": {
                    "type": "integer",
                    "description": "1 = helpful, -1 = not helpful, 0 = query-level only (no specific symbol). Omit if just reporting a bad query.",
                    "enum": [1, -1, 0],
                    "default": 0,
                },
                "original_score": {
                    "type": "number",
                    "description": "The original_score from the search result (optional)",
                    "default": 0.0,
                },
                "reason": {
                    "type": "string",
                    "description": "Why the result was helpful or not. For query-level feedback, explain what you expected but didn't find.",
                    "default": "",
                },
            },
            "required": ["query_text"],
        },
    },
    "get_reinforcement_stats": {
        "handler": get_reinforcement_stats,
        "description": inspect.getdoc(get_reinforcement_stats) or "",
        "schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "submit_ai_feedback": {
        "handler": submit_ai_feedback,
        "description": inspect.getdoc(submit_ai_feedback) or "",
        "schema": {
            "type": "object",
            "properties": {
                "client_id": {"type": "string", "description": "Identifier for your opencode client (optional)", "default": ""},
                "pr_context": {"type": "string", "description": "PR title, branch, or URL being reviewed", "default": ""},
                "tools_called": {
                    "type": "array",
                    "description": "List of MCP tools you called during this review",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string"},
                            "args": {"type": "object"},
                            "result_summary": {"type": "string"},
                        },
                    },
                    "default": [],
                },
                "results_used": {
                    "type": "array",
                    "description": "Which symbols/results you actually used and whether they were helpful",
                    "items": {
                        "type": "object",
                        "properties": {
                            "symbol_id": {"type": "string"},
                            "file_path": {"type": "string"},
                            "helpful": {"type": "boolean"},
                            "notes": {"type": "string"},
                        },
                    },
                    "default": [],
                },
                "results_expected": {"type": "string", "description": "What you expected to find but didn't", "default": ""},
                "quality_rating": {"type": "integer", "description": "1-5 overall rating of MCP tool output quality", "minimum": 1, "maximum": 5, "default": 0},
                "improvement_suggestions": {"type": "string", "description": "What could be better about the MCP tools or search results", "default": ""},
            },
            "required": [],
        },
    },
    "search_symbols_enhanced": {
        "handler": search_symbols_enhanced,
        "description": inspect.getdoc(search_symbols_enhanced) or "",
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Partial symbol name or file path"},
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 50},
                "search_files": {"type": "boolean", "default": True, "description": "Also search file paths"},
                "fuzzy": {"type": "boolean", "default": True, "description": "Enable fuzzy name matching"},
            },
            "required": ["query"],
        },
    },
    "analyze_pr_diff": {
        "handler": analyze_pr_diff,
        "description": inspect.getdoc(analyze_pr_diff) or "",
        "schema": {
            "type": "object",
            "properties": {
                "diff_text": {"type": "string", "description": "Full git diff text"},
                "max_symbols": {"type": "integer", "default": 10, "minimum": 1, "maximum": 30},
            },
            "required": ["diff_text"],
        },
    },
    "get_blast_radius_detailed": {
        "handler": get_blast_radius_detailed,
        "description": inspect.getdoc(get_blast_radius_detailed) or "",
        "schema": {
            "type": "object",
            "properties": {
                "symbol_id": {"type": "string", "description": "Full symbol path"},
            },
            "required": ["symbol_id"],
        },
    },
    "get_index_meta": {
        "handler": get_index_meta,
        "description": inspect.getdoc(get_index_meta) or "",
        "schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "get_symbols_in_file": {
        "handler": get_symbols_in_file,
        "description": inspect.getdoc(get_symbols_in_file) or "",
        "schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "File path like 'crates/common/src/redis/wrapper.rs'",
                },
            },
            "required": ["file_path"],
        },
    },
}

_DIRECTORY = {
    "server": _SERVER_INFO["name"],
    "access": "read-only",
    "description": "Code dependency graph index for blast-radius and traversal queries. No write access.",
    "tools": [
        {
            "name": "search_symbols",
            "endpoint": "/mcp",
            "method": "POST (MCP tools/call)",
            "description": "Search for symbols by partial name match. USE THIS FIRST when you don't know the exact symbol_id.",
            "arguments": {
                "query": "string (required) — partial name, e.g. 'InventoryClient', 'process_review'",
                "limit": "int (optional, default 20, max 50)",
            },
            "response_shape": {
                "query": "string",
                "matches": "int",
                "symbols": [{"symbol_id": "string", "short_name": "string", "has_calls": "bool", "has_uses": "bool", "has_callers": "bool", "has_users": "bool"}],
            },
        },
        {
            "name": "get_blast_radius",
            "endpoint": "/mcp",
            "method": "POST (MCP tools/call)",
            "description": "Get immediate callers, callees, type-users, and used-types for a symbol. Includes risk_factors (formula, thresholds, reasons), usage_modes per edge (pattern_match, construction, field_declaration, trait_impl, type_param, import), and optional usage_modes_filter to return only specific edge types.",
            "arguments": {
                "symbol_id": "string (required) — full symbol path, e.g. 'crates::inventory::client::InventoryClient'",
                "usage_modes_filter": "string[] (optional) — filter edges by usage mode, e.g. ['pattern_match', 'construction']",
            },
            "response_shape": {
                "symbol_id": "string",
                "kind": "string",
                "file_path": "string",
                "start_line": "int",
                "end_line": "int",
                "upstream": "string[] — functions that call this symbol",
                "downstream": "string[] — functions this symbol calls",
                "used_by": "string[] — types that use this symbol",
                "uses": "string[] — types this symbol uses",
                "used_by_modes": "object — {symbol_id: [usage_modes]} per used_by edge",
                "uses_modes": "object — {symbol_id: [usage_modes]} per uses edge",
                "usage_mode_summary": "object — {mode: count} across all edges",
                "total_connections": "int",
                "risk_score": "string — low|medium|high|critical",
                "risk_factors": "object — {score, total_connections, upstream_callers, downstream_callees, formula, reasons[]}",
            },
        },
        {
            "name": "batch_blast_radius",
            "endpoint": "/mcp",
            "method": "POST (MCP tools/call)",
            "description": "Get blast radius for multiple symbols at once. Returns combined impact analysis.",
            "arguments": {"symbol_ids": "string[] (required) — list of full symbol paths"},
            "response_shape": {
                "queried_symbols": "int",
                "results": "BlastRadius[]",
                "combined_impact": {"total_unique_callers": "int", "total_unique_callees": "int", "combined_risk_score": "string"},
            },
        },
        {
            "name": "get_symbol_content",
            "endpoint": "/mcp",
            "method": "POST (MCP tools/call)",
            "description": "Get the source code content of a symbol.",
            "arguments": {"symbol_id": "string (required) — full symbol path"},
            "response_shape": {
                "symbol_id": "string",
                "kind": "string",
                "file_path": "string",
                "start_line": "int",
                "end_line": "int",
                "content": "string — source code (max 8000 chars)",
                "truncated": "bool",
            },
        },
        {
            "name": "semantic_search",
            "endpoint": "/mcp",
            "method": "POST (MCP tools/call)",
            "description": "Search the codebase by meaning. Find all code related to a concept.",
            "arguments": {
                "query": "string (required) — natural language, e.g. 'payment validation'",
                "limit": "int (optional, default 10, max 25)",
            },
            "response_shape": {
                "query": "string",
                "matches": "int",
                "results": [{"symbol_id": "string", "score": "float", "file_path": "string", "content_preview": "string"}],
            },
        },
        {
            "name": "find_dependency_path",
            "endpoint": "/mcp",
            "method": "POST (MCP tools/call)",
            "description": "Find the shortest dependency path between two symbols.",
            "arguments": {
                "from_symbol": "string (required)",
                "to_symbol": "string (required)",
                "max_depth": "int (optional, default 5, max 10)",
            },
            "response_shape": {
                "from": "string",
                "to": "string",
                "path_found": "bool",
                "path_length": "int",
                "path": "string[]",
                "readable_path": "string — A -> B -> C",
            },
        },
        {
            "name": "traverse_graph",
            "endpoint": "/mcp",
            "method": "POST (MCP tools/call)",
            "description": "Multi-hop dependency traversal from a symbol up to N hops. Supports summary_only mode for compact output (counts per hop only).",
            "arguments": {
                "symbol_id": "string (required) — full symbol path",
                "depth": "int (optional, default 1, max 5) — how many hops to traverse",
                "summary_only": "bool (optional, default false) — return only counts, not full neighborhoods",
            },
            "response_shape": {
                "root_symbol": "string",
                "depth": "int",
                "symbols_visited": "int — number of neighborhoods returned (max 50)",
                "total_reachable_symbols": "int — unique symbols across all neighborhoods",
                "truncated": "bool — whether neighborhoods were capped at 50",
                "neighborhoods": "GraphNeighborhood[] — same shape as get_blast_radius output per symbol (omitted if summary_only=true)",
                "summary": "array — [{hop, symbol_id, upstream_count, downstream_count, used_by_count, uses_count}] (only if summary_only=true)",
            },
        },
        {
            "name": "get_graph_stats",
            "endpoint": "/mcp",
            "method": "POST (MCP tools/call)",
            "description": "Get total graph nodes and edges. No arguments needed.",
            "arguments": {},
            "response_shape": {
                "graph_nodes": "int",
                "graph_edges": "int",
            },
        },
        {
            "name": "submit_search_feedback",
            "endpoint": "/mcp",
            "method": "POST (MCP tools/call)",
            "description": "Submit feedback on semantic_search results to improve future search quality. Call after reviewing results — +1 for helpful, -1 for not helpful.",
            "arguments": {
                "query_text": "string (required) — the original search query",
                "symbol_id": "string (required) — symbol from the result being rated",
                "feedback": "int (required) — 1 or -1",
                "original_score": "float (optional) — from the search result",
                "reason": "string (optional) — explanation",
            },
            "response_shape": {
                "status": "string",
                "query_text": "string",
                "symbol_id": "string",
                "feedback": "int",
            },
        },
        {
            "name": "get_reinforcement_stats",
            "endpoint": "/mcp",
            "method": "POST (MCP tools/call)",
            "description": "Get reinforcement learning statistics — feedback count, boosted/penalized symbols, query expansions.",
            "arguments": {},
            "response_shape": {
                "total_feedback": "int",
                "boosted_symbols": "int",
                "penalized_symbols": "int",
                "query_expansions": "int",
                "top_adjusted_symbols": "array",
            },
        },
        {
            "name": "submit_ai_feedback",
            "endpoint": "/mcp",
            "method": "POST (MCP tools/call)",
            "description": "Submit full post-analysis feedback summary after PR review. This is the PRIMARY feedback mechanism — call after your entire review.",
            "arguments": {
                "client_id": "string (optional) — your client identifier",
                "pr_context": "string (optional) — PR title or URL",
                "tools_called": "array (optional) — [{tool, args, result_summary}]",
                "results_used": "array (optional) — [{symbol_id, file_path, helpful, notes}]",
                "results_expected": "string (optional) — what you expected but didn't find",
                "quality_rating": "int (optional, 1-5) — overall MCP quality rating",
                "improvement_suggestions": "string (optional) — what could be better",
            },
            "response_shape": {
                "feedback_id": "string",
                "status": "string — pending",
            },
        },
        {
            "name": "search_symbols_enhanced",
            "endpoint": "/mcp",
            "method": "POST (MCP tools/call)",
            "description": "Enhanced symbol search with file path matching and fuzzy name resolution. Use when search_symbols returns 0 matches — searches file paths, short names, and partial matches.",
            "arguments": {
                "query": "string (required) — partial symbol name or file path, e.g. 'deactivate_customer', 'dormancy_report.rs'",
                "limit": "int (optional, default 20, max 50)",
                "search_files": "bool (optional, default true) — also search file paths",
                "fuzzy": "bool (optional, default true) — enable fuzzy name matching",
            },
            "response_shape": {
                "query": "string",
                "matches": "int",
                "symbols": [{"symbol_id": "string", "short_name": "string", "file_path": "string", "match_type": "string — exact|fuzzy|file_path"}],
            },
        },
        {
            "name": "analyze_pr_diff",
            "endpoint": "/mcp",
            "method": "POST (MCP tools/call)",
            "description": "Analyze a git diff and return changed symbols with blast radius AND change classification in one call. Parses diff for function/struct/trait names, classifies change types (new, signature_change, body_only, trait_impl, struct_field, deleted), resolves them against the graph, and gets blast radius for each.",
            "arguments": {
                "diff_text": "string (required) — full git diff text",
                "max_symbols": "int (optional, default 10, max 30) — max symbols to analyze",
            },
            "response_shape": {
                "changed_files": "string[]",
                "resolved_symbols": [{"symbol_id": "string", "change_type": "string — new|signature_change|body_only|trait_impl|struct_field|deleted", "change_details": "string", "risk_score": "string", "risk_factors": "object", "total_connections": "int"}],
                "new_symbols_not_in_index": "array",
                "deleted_symbols": "array",
                "high_risk_symbols": "array — symbols with high/critical risk",
                "change_type_breakdown": "object — {change_type: count}",
                "summary": "object",
            },
        },
        {
            "name": "get_blast_radius_detailed",
            "endpoint": "/mcp",
            "method": "POST (MCP tools/call)",
            "description": "Get blast radius with confidence scores AND usage modes on each edge. Confidence: 'high' = direct caller/callee, 'medium' = type reference. Usage modes: pattern_match, construction, field_declaration, trait_impl, type_param, import, type_reference. Use when you need to know how each caller uses the symbol.",
            "arguments": {"symbol_id": "string (required) — full symbol path"},
            "response_shape": {
                "symbol_id": "string",
                "risk_score": "string — low|medium|high|critical",
                "risk_factors": "object — {score, total_connections, upstream_callers, downstream_callees, formula, reasons[]}",
                "upstream": [{"symbol_id": "string", "confidence": "string — high|medium|low", "edge_type": "calls"}],
                "downstream": [{"symbol_id": "string", "confidence": "string — high|medium|low", "edge_type": "calls"}],
                "used_by": [{"symbol_id": "string", "confidence": "string — high|medium|low", "edge_type": "type_reference", "usage_modes": "string[] — pattern_match|construction|field_declaration|trait_impl|type_param|import|type_reference"}],
                "uses": [{"symbol_id": "string", "confidence": "string — high|medium|low", "edge_type": "type_reference", "usage_modes": "string[]"}],
                "total_connections": "int",
                "confidence_summary": "object — {high_confidence_edges, medium_confidence_edges, overall_confidence}",
            },
        },
        {
            "name": "get_index_meta",
            "endpoint": "/mcp",
            "method": "POST (MCP tools/call)",
            "description": "Get graph build metadata — build timestamp, commit hash, classifier version, gen number, collection info. Use this to verify the graph is fresh and which classifier version was used.",
            "arguments": {},
            "response_shape": {
                "graph_gen": "int — current generation number",
                "classifier_version": "string — usage mode classifier version",
                "last_indexed_commit": "string — short commit hash",
                "current_head": "string — short commit hash",
                "up_to_date": "bool — whether graph matches HEAD",
                "snapshot_created_at": "string — ISO timestamp",
                "files_indexed": "int",
                "total_symbols": "int",
                "total_edges": "int",
                "weaviate_collection": "string — active collection name",
                "embedding_model": "string",
                "embedding_dimensions": "int",
                "features": "string[] — list of enabled features",
            },
        },
        {
            "name": "get_symbols_in_file",
            "endpoint": "/mcp",
            "method": "POST (MCP tools/call)",
            "description": "List all symbols defined in a file. Use to resolve a diff's file path to exact symbols — avoids guessing symbol names.",
            "arguments": {"file_path": "string (required) — e.g. 'crates/common/src/redis/wrapper.rs'"},
            "response_shape": {
                "file_path": "string",
                "symbols_found": "int",
                "symbols": [{"symbol_id": "string", "kind": "string", "start_line": "int", "end_line": "int"}],
            },
        },
    ],
    "rest_endpoints": {
        "GET /blast-radius/{symbol_id}": "Direct REST blast radius query (port 8000)",
        "GET /traverse/{symbol_id}?depth=2": "Direct REST traversal query (port 8000)",
        "GET /stats": "Graph stats (port 8000)",
        "GET /has/{symbol_id}": "Check if symbol exists in graph (port 8000)",
    },
    "usage": "POST to /mcp with MCP JSON-RPC protocol (stateless, no session required).",
}


def _jsonrpc_response(req_id: Any, result: Any) -> Response:
    payload = {"jsonrpc": "2.0", "id": req_id, "result": result}
    return Response(
        content=f"event: message\ndata: {json.dumps(payload)}\n\n",
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


def _jsonrpc_error(req_id: Any, code: int, message: str) -> Response:
    payload = {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
    return Response(
        content=f"event: message\ndata: {json.dumps(payload)}\n\n",
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


def _jsonrpc_response_plain(req_id: Any, result: Any) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})


async def _handle_mcp(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status_code=400)

    req_id = body.get("id")
    method = body.get("method", "")

    accept = request.headers.get("accept", "")
    wants_sse = "text/event-stream" in accept

    def _respond(req_id: Any, result: Any) -> Response:
        if wants_sse:
            return _jsonrpc_response(req_id, result)
        return _jsonrpc_response_plain(req_id, result)

    if method == "initialize":
        params = body.get("params", {})
        client_pv = params.get("protocolVersion", "")
        supported_versions = {"2024-11-05", "2025-03-26", "2025-06-18"}
        negotiated_pv = client_pv if client_pv in supported_versions else _PROTOCOL_VERSION

        result = {
            "protocolVersion": negotiated_pv,
            "capabilities": _CAPABILITIES,
            "serverInfo": _SERVER_INFO,
        }
        return _respond(req_id, result)
    if method == "tools/list":
        tools = []
        for name, spec in _TOOLS.items():
            tools.append({
                "name": name,
                "description": spec["description"],
                "inputSchema": spec["schema"],
            })
        return _respond(req_id, {"tools": tools})

    if method == "tools/call":
        params = body.get("params", {})
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        if tool_name not in _TOOLS:
            return _respond(req_id, {
                "content": [{"type": "text", "text": json.dumps({"error": f"Unknown tool: {tool_name}"})}],
                "isError": True,
            })

        handler = _TOOLS[tool_name]["handler"]
        try:
            if asyncio.iscoroutinefunction(handler):
                result = await handler(**arguments)
            else:
                result = await asyncio.to_thread(handler, **arguments)
            return _respond(req_id, {
                "content": [{"type": "text", "text": json.dumps(result, default=str)}],
                "isError": False,
            })
        except Exception as exc:
            logger.exception("Tool %s failed", tool_name)
            return _respond(req_id, {
                "content": [{"type": "text", "text": json.dumps({"error": str(exc), "traceback": traceback.format_exc()[:2000]})}],
                "isError": True,
            })

    if method == "ping":
        return _respond(req_id, {})

    return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")


async def _tool_directory(request: Request) -> JSONResponse:
    return JSONResponse(_DIRECTORY)


async def _health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "server": _SERVER_INFO["name"]})


_routes = [
    Route("/", _tool_directory, methods=["GET"]),
    Route("/mcp", _handle_mcp, methods=["POST", "GET"]),
    Route("/health", _health, methods=["GET"]),
]


class BearerTokenAuthMiddleware(BaseHTTPMiddleware):
    """Constant-time bearer token auth with IP-based rate limiting.

    Security measures:
    - hmac.compare_digest for constant-time comparison (prevents timing attacks)
    - Per-IP rate limiting on failed attempts (max 5 per 60s, then 5min lockout)
    - All denied attempts logged with client IP and timestamp
    - /health endpoint is exempt (so monitoring works without a token)
    """

    _failed_attempts: dict[str, list[float]] = {}
    _MAX_ATTEMPTS = 5
    _WINDOW_SEC = 60
    _LOCKOUT_SEC = 300

    def _is_rate_limited(self, client_ip: str) -> bool:
        import time
        now = time.time()
        attempts = self._failed_attempts.get(client_ip, [])
        attempts = [t for t in attempts if now - t < self._LOCKOUT_SEC]
        self._failed_attempts[client_ip] = attempts
        recent = [t for t in attempts if now - t < self._WINDOW_SEC]
        return len(recent) >= self._MAX_ATTEMPTS

    def _record_failure(self, client_ip: str) -> None:
        import time
        self._failed_attempts.setdefault(client_ip, []).append(time.time())

    def _notify_new_client_if_needed(self, client_ip: str, request: Request) -> None:
        """Send an email the first time an IP successfully authenticates."""
        try:
            import redis as redis_lib
            from app.core.config import settings

            if not settings.redis_url:
                return

            r = redis_lib.Redis.from_url(settings.redis_url, decode_responses=True)
            key = "mcp:known_client_ips"
            is_new = r.sadd(key, client_ip) == 1
            # Refresh TTL on every authenticated request so the set stays
            # alive while in use — prevents mass re-notifications after expiry.
            r.expire(key, 60 * 60 * 24 * 90)
            if is_new:
                from app.core.notifications import notify_new_client
                notify_new_client(
                    client_ip=client_ip,
                    path=str(request.url.path),
                    user_agent=request.headers.get("user-agent", ""),
                )
        except Exception:
            logger.debug("New-client notification check failed", exc_info=True)

    async def dispatch(self, request: Request, call_next):
        from app.core.config import settings

        if request.url.path == "/health":
            return await call_next(request)

        token = settings.mcp_auth_token
        if not token:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"

        if self._is_rate_limited(client_ip):
            logger.warning("Auth rate-limited for IP %s — possible brute force", client_ip)
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": "Too many failed attempts"}},
                status_code=429,
                headers={"Retry-After": str(self._LOCKOUT_SEC)},
            )

        import hmac
        auth = request.headers.get("authorization", "")
        provided = ""
        if auth.startswith("Bearer "):
            provided = auth[7:]

        if hmac.compare_digest(provided, token):
            self._notify_new_client_if_needed(client_ip, request)
            return await call_next(request)

        self._record_failure(client_ip)
        logger.warning("Auth failed for IP %s (path=%s)", client_ip, request.url.path)
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": "Unauthorized"}},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )


_app = Starlette(
    routes=_routes,
    middleware=[Middleware(BearerTokenAuthMiddleware)],
)


def create_readonly_mcp_server():
    """Return the Starlette app. Kept for API compatibility with main.py."""
    return _app


readonly_mcp_server = _app
