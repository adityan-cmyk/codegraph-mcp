"""Read-only graph MCP tools for opencode agent consumption.

These tools expose ONLY graph read operations and symbol content.
No write, index, file, cargo, or mutation operations are accessible.
"""

import logging
import threading
import time
import uuid

from app.core.config import settings as _settings
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


# std trait impls and per-module helper clones: appear in (nearly) every
# module, so they carry no drift signal — diff_modules excludes them from
# its score (a 0.70 "drift" was 20x one helper + From/from impls)
_BOILERPLATE_SYMBOL_NAMES = frozenset({
    "from", "tryfrom", "display", "debug", "clone", "partial_eq", "eq",
    "hash", "serialize", "deserialize", "default", "as_ref", "borrow",
    "deref", "into", "to_string", "into_iter", "from_iter", "iterator",
    "ist_day_start_utc", "now_utc", "now_naive_utc",
})


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
    if not query or not query.strip():
        return {"error": "query must be a non-empty symbol-name fragment", "query": query}
    results = _filter_noise(graph_index.search_symbols(query, limit=max(limit * 2, 50)))[:limit]
    return {
        "query": query,
        "matches": len(results),
        "symbols": results,
    }


def search_symbols_batch(queries: list[str], limit: int = 5) -> dict[str, object]:
    """Resolve many partial names in ONE call — same per-name results as search_symbols, but one round trip instead of N. Use this whenever you have a list of names (imports from a diff, field names, call targets). Provide queries: list of partial names (e.g. ['lookup_txn', 'check_if_txn', 'find_by_ref_id']). Optional: limit per query (default 5, max 20)."""
    if not queries or not isinstance(queries, list):
        return {"error": "queries must be a non-empty list of name fragments"}
    limit = max(1, min(int(limit or 5), 20))
    results: dict[str, list] = {}
    unresolved: list[str] = []
    for q in queries[:40]:
        q = str(q).strip()
        if not q:
            continue
        hits = _filter_noise(graph_index.search_symbols(q, limit=max(limit * 2, 30)))[:limit]
        results[q] = hits
        if not hits:
            unresolved.append(q)
    return {
        "queries_resolved": len(results),
        "results": results,
        "unresolved": unresolved,
        "calls_saved": max(0, len(results) - 1),
    }


def find_field_access(struct_name: str, field: str) -> dict[str, object]:
    """Find readers and writers of a struct field — 'who reads ChargebackTxn.transaction_ref_id' without grep. Scans every indexed symbol body for field accesses: readers touch `.field`, writers assign `.field =` or construct `field: value`. Provide struct_name (e.g. 'ChargebackTxn' — '' to scan all structs) and field (e.g. 'transaction_ref_id'). Returns per-site symbol_id/file/line/snippet so you can jump to the exact access. Read-only, safe to call anytime."""
    field = (field or "").strip()
    if not field:
        return {"error": "field is required"}
    struct_name = (struct_name or "").strip()

    import re as _re

    reader_re = _re.compile(rf"\.\s*{_re.escape(field)}\b(?!\s*[:=])")
    writer_re = _re.compile(rf"\.\s*{_re.escape(field)}\s*=")
    # struct-literal init `field: value` — but NOT a type-annotation decl
    # (`field: String`), which is the field declaration itself.
    init_re = _re.compile(rf"\b{_re.escape(field)}\s*:\s*(?:self\b|&[a-z]|[a-z0-9_\"'{{])")
    decl_re = _re.compile(rf"\b{_re.escape(field)}\s*:\s*(?:[A-Z]|&[A-Z]|Option<|Vec<|Box<|HashMap|HashSet|dyn )")

    readers: list[dict[str, object]] = []
    writers: list[dict[str, object]] = []
    for chunk in _iter_snapshot_chunks():
        if struct_name and struct_name not in chunk.symbol_id and struct_name not in chunk.content[:2000]:
            continue
        lines = chunk.content.split("\n")
        for offset, line in enumerate(lines):
            if field not in line:
                continue
            lineno = chunk.start_line + offset
            base = {
                "symbol_id": chunk.symbol_id,
                "file_path": chunk.file_path,
                "line": lineno,
                "snippet": line.strip()[:200],
            }
            if writer_re.search(line):
                writers.append({**base, "access": "write"})
            elif decl_re.search(line) and not init_re.search(line):
                continue  # the field declaration itself
            elif init_re.search(line):
                writers.append({**base, "access": "construct"})
            elif reader_re.search(line):
                readers.append({**base, "access": "read"})

    return {
        "struct_name": struct_name or "(any)",
        "field": field,
        "readers_count": len(readers),
        "writers_count": len(writers),
        "readers": readers[:40],
        "writers": writers[:40],
        "truncated": max(0, len(readers) + len(writers) - 80),
    }


def find_string_producers(pattern: str, limit: int = 30) -> dict[str, object]:
    """Find every symbol that produces a string literal containing the given prefix/fragment — catches two producers of the same format disagreeing (a Rust format!("UT-TXN-…") vs a SQL 'TXN-…' literal is exactly the bug class that ships). Searches Rust format!/string literals AND SQL chunks (tables, migrations). Provide pattern (e.g. 'TXN-', 'UT-TXN'). Returns producing symbol_id, the literal, and whether it sits inside format!. Read-only, safe to call anytime."""
    pattern = (pattern or "").strip()
    if not pattern:
        return {"error": "pattern is required"}
    import re as _re

    # Double-quoted literals FIRST, single-quoted only outside their spans —
    # a Rust &'static lifetime otherwise swallows the string that follows it
    # (`-> &'static str { "…TXN-…" }` matched as one bogus '…' literal).
    dq_re = _re.compile(r"b?\"(?:[^\"\\]|\\.)*\"")
    sq_re = _re.compile(r"'(?:[^'\\]|\\.)*'")

    def _literals(content: str) -> list[tuple[int, str]]:
        spans: list[tuple[int, str]] = []
        covered: list[tuple[int, int]] = []
        for m in dq_re.finditer(content):
            spans.append((m.start(), m.group(0)))
            covered.append((m.start(), m.end()))
        for m in sq_re.finditer(content):
            if any(s <= m.start() < e for s, e in covered):
                continue
            spans.append((m.start(), m.group(0)))
        return spans

    producers: list[dict[str, object]] = []
    limit = max(1, min(int(limit or 30), 100))
    for chunk in _iter_snapshot_chunks():
        for start, literal in _literals(chunk.content):
            if pattern not in literal:
                continue
            line = chunk.start_line + chunk.content[:start].count("\n")
            prefix = chunk.content[max(0, start - 120): start]
            producers.append({
                "symbol_id": chunk.symbol_id,
                "kind": chunk.kind,
                "file_path": chunk.file_path,
                "line": line,
                "literal": literal[:160],
                "in_format_macro": "format!" in prefix,
            })
            if len(producers) >= limit:
                break
        if len(producers) >= limit:
            break

    distinct = {(p["symbol_id"], p["literal"]) for p in producers}
    return {
        "pattern": pattern,
        "producers_count": len(producers),
        "distinct_symbol_literal_pairs": len(distinct),
        "producers": producers,
        "note": (
            "multiple distinct literals for one prefix usually means two producers disagree — "
            "compare them before shipping"
            if len({p["literal"] for p in producers}) > 1
            else "single literal form across all producers"
        ),
    }


def _iter_snapshot_chunks():
    from app.core.index_store import index_metadata_store

    snap = index_metadata_store.load_snapshot()
    return snap.chunks if snap else []


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


def get_tests_for_symbol(symbol_id: str, depth: int = 1, include_assertions: bool = True) -> dict[str, object]:
    """Find the tests that cover a symbol — test fns that CALL it or reference its type (USES), from symbols flagged is_test (#[test]/#[tokio::test]/#[cfg(test)] modules). Use this before changing a symbol to see if existing tests will catch regressions. Transitive coverage matters: a symbol may only be exercised through a wrapper (depth=2 finds tests of its callers). Assertion lines from each matched test are returned so you can see WHAT is verified, not just that a test exists. Provide the full symbol_id (e.g. 'crates::wallet::settle::process_payment'). Optional: depth=1..3 (default 1 — direct tests only; 2 includes tests of direct callers, 3 one more hop), include_assertions=false to skip reading test bodies. Read-only, safe to call anytime."""
    symbol_id = symbol_id.strip()
    if not symbol_id:
        return {"error": "symbol_id is required"}
    depth = max(1, min(int(depth or 1), 3))

    test_rows = graph_index.get_tests_for(symbol_id)
    direct = [t for t in test_rows if t["relation"] == "calls"]
    mention_only = [t for t in test_rows if t["relation"] == "type_reference"]

    # Transitive coverage: tests that exercise this symbol through a caller
    # (vehicle_registration_tests -> process_transaction -> fetch_transaction_id
    # showed zero tests at depth 1 while the suite DID cover it).
    transitive: list[dict[str, object]] = []
    seen_targets = {symbol_id}
    frontier = set(_callers_of(symbol_id))
    for hop in range(2, depth + 1):
        if not frontier:
            break
        next_frontier: set[str] = set()
        for mid in sorted(frontier):
            for t in graph_index.get_tests_for(mid):
                if t["relation"] != "calls":
                    continue
                transitive.append({
                    "symbol_id": t["symbol_id"],
                    "relation": "calls",
                    "file_path": t.get("file_path", ""),
                    "start_line": t.get("start_line", 0),
                    "end_line": t.get("end_line", 0),
                    "depth": hop,
                    "via_chain": [mid],
                })
            next_frontier |= _callers_of(mid)
        seen_targets |= frontier
        frontier = next_frontier - seen_targets

    matched = direct + mention_only + transitive
    files = sorted({t["file_path"] for t in matched if t["file_path"]})

    # Assertion lines: the no-MCP reviewer beat the graph run because it READ
    # the assertion (RefId.eq(&unsettled_txn.ref_id)) — surface them here.
    assertions: list[dict[str, object]] = []
    if include_assertions:
        chain_names = _short_name(symbol_id)
        for m in transitive:
            for mid in m.get("via_chain", []):
                chain_names |= _short_name(mid)
        for t in matched:
            for line_info in _extract_assertions(t):
                mentions = any(name and name in line_info["code"] for name in chain_names)
                line_info["mentions_chain"] = mentions
                line_info["test_symbol_id"] = t["symbol_id"]
                assertions.append(line_info)

    all_direct = len(direct) > 0
    any_transitive = len(transitive) > 0
    if all_direct:
        note = "direct test callers exist — regressions here are likely caught"
    elif any_transitive:
        note = (
            "no test calls this symbol directly, but tests exercise it through its callers "
            f"(depth<= {depth}) — read the assertions to confirm they cover your change"
        )
    else:
        note = "no test directly calls this symbol — changes are unverified by the suite"

    return {
        "symbol_id": symbol_id,
        "depth_used": depth,
        "tests_calling": direct,
        "tests_referencing_type": mention_only,
        "tests_transitive": transitive,
        "test_count": len(matched),
        "test_files": files,
        "assertions": assertions[:30],
        "assertions_truncated": max(0, len(assertions) - 30),
        "coverage_note": note,
    }


def _callers_of(symbol_id: str) -> set[str]:
    neighborhood = (
        graph_index.get_blast_radius(symbol_id)
        if hasattr(graph_index, "get_blast_radius")
        else graph_index.get_neighbors(symbol_id)
    )
    result = neighborhood.model_dump() if hasattr(neighborhood, "model_dump") else neighborhood
    return {
        e["symbol_id"] if isinstance(e, dict) else e
        for e in result.get("upstream", [])
        if (e["symbol_id"] if isinstance(e, dict) else e)
    }


def _short_name(symbol_id: str) -> set[str]:
    parts = symbol_id.split("::")
    names = {parts[-1]} if parts else set()
    if len(parts) >= 2:
        names.add(f"{parts[-2]}::{parts[-1]}")
    return names


_ASSERT_RE = None


def _extract_assertions(test_row: dict) -> list[dict[str, object]]:
    """assert/expect/panic lines from a test symbol's source span on disk."""
    global _ASSERT_RE
    if _ASSERT_RE is None:
        import re as _re

        _ASSERT_RE = _re.compile(
            r"\b(assert!|assert_eq!|assert_ne!|assert_matches!|debug_assert|should_panic|expect\(|\.unwrap\(|panic!)"
        )

    meta = _get_symbol_metadata(test_row["symbol_id"]) or {}
    file_path = meta.get("file_path") or test_row.get("file_path") or ""
    start = int(meta.get("start_line") or test_row.get("start_line") or 0)
    end = int(meta.get("end_line") or test_row.get("end_line") or 0)
    if not file_path or not start:
        return []

    from pathlib import Path

    from app.core.config import settings

    root = Path(settings.codebase_root_path or "/repos/codebase").resolve()
    try:
        target = (root / file_path).resolve()
        # file_path comes from the index, not the client — still refuse any
        # resolved path that escapes the repo root (defense in depth).
        if not target.is_relative_to(root):
            return []
        lines = target.read_text(errors="replace").splitlines()
    except OSError:
        return []

    out: list[dict[str, object]] = []
    for lineno in range(max(start, 1), min(end, len(lines)) + 1):
        raw = lines[lineno - 1]
        if _ASSERT_RE.search(raw):
            out.append({
                "file_path": file_path,
                "line": lineno,
                "code": raw.strip()[:240],
            })
    return out


def get_blast_radius(symbol_id: str, usage_modes_filter: list[str] | None = None, summary_only: bool = False, exclude_shared_state_types: bool = True) -> dict[str, object]:
    """Get the immediate blast radius of a symbol — direct callers (CALLS edges), callees, type-references (USES edges), and used types. Callers are functions that CALL this symbol; type-references are symbols that merely mention the type (fields, params) — they are reported separately and never conflated. Use this during PR review to understand the impact of changing a symbol. Provide the full symbol_id (e.g. 'crates::inventory::client::InventoryClient'). Optional: usage_modes_filter=['pattern_match', 'construction'] to return only callers that pattern-match or construct this type; summary_only=true for counts grouped by module + risk score; exclude_shared_state_types=true (default) filters god-types (GlobalState-like, >50 type-references) from the type expansion — set false to see them."""
    if not graph_index.has_symbol(symbol_id):
        return {"error": f"Symbol '{symbol_id}' not found in the graph index.", "symbol_id": symbol_id, "hint": "Use search_symbols to find the correct symbol_id."}

    from app.core.query_cache import cache_get, cache_set

    cache_args = {"symbol_id": symbol_id, "usage_modes_filter": usage_modes_filter,
                  "summary_only": summary_only, "exclude_shared_state_types": exclude_shared_state_types}
    cached = cache_get("blast_radius", cache_args)
    if cached is not None:
        cached["cache"] = "hit"
        return cached

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
    # the same symbol legitimately shows different counts in different tools —
    # label what each measures so nobody has to guess which is "right"
    result["count_semantics"] = {
        "total_connections": "raw union of all four edge directions (test callers included, unweighted)",
        "connection_breakdown": "raw count per edge direction",
        "risk_factors": "EFFECTIVE counts: test callers excluded, visibility-weighted (private 0.4x, pub(crate) 0.7x, pub 1.0x)",
        "traverse_graph_hop1_hotspots": "unique 1-hop neighbors, deduplicated across edge kinds",
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
        summary = _attach_stale_warning(result)
        cache_set("blast_radius", cache_args, summary, ttl=3600, tags=["graph"])
        return summary

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

    final = _attach_stale_warning(result)
    cache_set("blast_radius", cache_args, final, ttl=3600, tags=["graph"])
    return final


def batch_blast_radius(symbol_ids: list[str], summary_only: bool = False, resolve_names: bool = False) -> dict[str, object]:
    """Get blast radius for multiple symbols in a single call. Use this when a PR changes several symbols and you need the combined impact. Provide a list of symbol_ids (e.g. ['crates::wallet::transfer', 'crates::wallet::validate']). Optional: summary_only=true returns counts + risk per symbol WITHOUT the full neighbor lists (use when you only need sizes — full lists for 15+ symbols are huge); resolve_names=true accepts partial names and resolves each via search first (saves a separate search_symbols call per name)."""
    results: list[dict[str, object]] = []
    all_downstream: set[str] = set()
    all_upstream: set[str] = set()
    all_uses: set[str] = set()
    all_used_by: set[str] = set()
    resolved_names: dict[str, str] = {}

    for sid in symbol_ids:
        if resolve_names and not graph_index.has_symbol(sid):
            hits = _filter_noise(graph_index.search_symbols(sid, limit=10))
            if hits:
                resolved_names[sid] = hits[0]["symbol_id"]
                sid = hits[0]["symbol_id"]
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
        if summary_only:
            results.append({
                "symbol_id": br["symbol_id"],
                "risk_score": br["risk_score"],
                "risk_factors": br.get("risk_factors", {}),
                "total_connections": br["total_connections"],
                "connection_breakdown": br.get("connection_breakdown", {}),
                "usage_mode_summary": br.get("usage_mode_summary", {}),
            })
        else:
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

    changed_set = set(symbol_ids) | set(resolved_names.values())
    combined_downstream = all_downstream - changed_set
    combined_upstream = all_upstream - changed_set
    combined_uses = all_uses - changed_set
    combined_used_by = all_used_by - changed_set

    combined: dict[str, object] = {
        "total_unique_callers": len(combined_upstream),
        "total_unique_callees": len(combined_downstream),
        "total_unique_type_users": len(combined_used_by),
        "total_unique_types_used": len(combined_uses),
        "combined_risk_score": _compute_risk_score(
            list(combined_upstream), list(combined_downstream),
            list(combined_used_by), list(combined_uses),
        ),
    }
    if not summary_only:
        combined.update({
            "all_callers": sorted(combined_upstream)[:_MAX_PER_DIRECTION],
            "all_callees": sorted(combined_downstream)[:_MAX_PER_DIRECTION],
            "all_type_users": sorted(combined_used_by)[:_MAX_PER_DIRECTION],
            "all_types_used": sorted(combined_uses)[:_MAX_PER_DIRECTION],
        })

    out: dict[str, object] = {
        "queried_symbols": len(symbol_ids),
        "summary_only": summary_only,
        "results": results,
        "combined_impact": combined,
    }
    if resolved_names:
        out["resolved_names"] = resolved_names
    return out


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
                logger.debug("non-fatal error swallowed", exc_info=True)
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
                # chunk.content carries an index-time enrichment prefix
                # ('// summary', '// Imports: ...') that is NOT source — an
                # agent pasting content back as code would be wrong. Split it
                # into its own field; also fixes line-offset paging, which the
                # prefix's extra lines silently shifted.
                src_lines = chunk.content.split("\n")
                enrich_lines: list[str] = []
                while src_lines and src_lines[0].startswith("// "):
                    enrich_lines.append(src_lines.pop(0))
                if src_lines and src_lines[0] == "":
                    src_lines.pop(0)
                enrichment = "\n".join(enrich_lines) or None
                content = "\n".join(src_lines)
                if start_line is not None or end_line is not None:
                    lo = max(start_line or chunk.start_line, chunk.start_line)
                    hi = min(end_line or chunk.end_line, chunk.end_line)
                    if lo > hi:
                        return {"error": f"line range [{lo}, {hi}] outside symbol range [{chunk.start_line}, {chunk.end_line}]", "symbol_id": symbol_id}
                    page_lines = content.split("\n")[(lo - chunk.start_line):(hi - chunk.start_line + 1)]
                    return {
                        "symbol_id": chunk.symbol_id,
                        "kind": chunk.kind,
                        "file_path": chunk.file_path,
                        "start_line": lo,
                        "end_line": hi,
                        "symbol_span": [chunk.start_line, chunk.end_line],
                        "total_lines": chunk.end_line - chunk.start_line + 1,
                        "content": "\n".join(page_lines)[:16000],
                        "enrichment_summary": enrichment,
                        "truncated": (hi - lo + 1) > 16000 // 40,
                    }
                return {
                    "symbol_id": chunk.symbol_id,
                    "kind": chunk.kind,
                    "file_path": chunk.file_path,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "total_lines": chunk.end_line - chunk.start_line + 1,
                    "content": content[:8000],
                    "enrichment_summary": enrichment,
                    "truncated": len(content) > 8000,
                    "hint": "content truncated — re-call with start_line/end_line to page" if len(content) > 8000 else None,
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
    """Get statistics about the reinforcement learning system — how much feedback has been collected, which symbols are boosted or penalized, query expansion count, and per-tool win attribution (which tool directly produced each finding across accepted reviews). Read-only, safe to call anytime."""
    try:
        from app.rag.reinforcement import feedback_store
        stats = feedback_store.get_reinforcement_stats()
        try:
            from app.rag.reinforcement.ai_feedback_store import get_tool_win_stats

            stats["tool_wins"] = get_tool_win_stats()
        except Exception:
            stats["tool_wins"] = {"error": "tool win stats unavailable"}
        return stats
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
    tool_wins: list[dict] | None = None,
) -> dict[str, object]:
    """Submit a full post-analysis feedback summary after completing a PR review. This is the PRIMARY feedback mechanism — call this after your entire review is done with all tools you called, which results were helpful, what you expected but didn't find, a quality rating (1-5), and improvement suggestions. Also pass tool_wins: [{tool, finding, symbol_id?}] — attribute each review finding to the tool that directly produced it, so per-tool value can be measured across reviews (an overall rating can't tell which tool earned it). The feedback goes through a quality gate (only specific, actionable feedback is accepted) and at 10 accepted feedbacks, a new build is auto-triggered. This tool replaces the HTTP POST to /api/feedback/ai — use this MCP tool instead."""
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
            tool_wins=tool_wins or [],
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
        # A dead end should still point somewhere: reverse-direction and
        # nearest-common-neighbor hints turn "no path" into a lead (e.g. a
        # job queue sits between the two symbols — no synchronous path).
        # Hints are garnish: exhaustive shortestPath exploration on
        # DISCONNECTED pairs can take minutes — never let garnish block the
        # no-path answer (regression: dead ends timed out at any depth).
        import concurrent.futures as _fut

        try:
            with _fut.ThreadPoolExecutor(max_workers=1) as _ex:
                hints = _ex.submit(_dependency_dead_end_hints, from_symbol, to_symbol, max_depth).result(timeout=5.0)
        except Exception:
            hints = {}
        return {
            "from": from_symbol,
            "to": to_symbol,
            "path_found": False,
            "message": f"No path found within {max_depth} hops.",
            **hints,
        }

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


def _dependency_dead_end_hints(from_symbol: str, to_symbol: str, max_depth: int) -> dict[str, object]:
    """Reverse-path existence + nearest common neighbor for failed lookups."""
    hints: dict[str, object] = {}
    try:
        backend = graph_index._active()
        if not hasattr(backend, "_get_driver"):
            return hints
        gen = backend._gen
        with backend._get_driver().session() as session:
            rev = session.run(
                f"""
                MATCH path = shortestPath(
                    (start:Symbol {{id: $to_id, gen: $gen}})-[:CALLS*1..{max_depth + 3}]->(end:Symbol {{id: $from_id, gen: $gen}})
                )
                RETURN count(path) AS n
                """,
                to_id=to_symbol, from_id=from_symbol, gen=gen,
            ).single()
            if rev and rev["n"]:
                hints["reverse_path_exists"] = (
                    f"'{to_symbol.split('::')[-1]}' reaches '{from_symbol.split('::')[-1]}' — the dependency "
                    "runs opposite to the queried direction"
                )
            # nearest common neighbor: two BOUNDED 2-hop neighborhoods
            # intersected in memory — the single undirected 3+3-hop cartesian
            # pattern took 237s on the live graph (unacceptable on a tool path)
            def _call_neighborhood(sid: str) -> set[str]:
                rows = session.run(
                    """
                    MATCH (s:Symbol {id: $sid, gen: $gen})-[:CALLS*1..2]-(n:Symbol)
                    WHERE n.gen = $gen AND n.id <> $sid
                      AND NOT n.id ENDS WITH 'GlobalState' AND NOT n.id ENDS WITH 'FastagGlobalState'
                    RETURN DISTINCT n.id AS id LIMIT 400
                    """,
                    sid=sid, gen=gen,
                )
                return {r["id"] for r in rows}

            near = sorted(_call_neighborhood(from_symbol) & _call_neighborhood(to_symbol))[:3]
            if near:
                hints["nearest_common_neighbors"] = near
                hints["note"] = "no direct path — these shared neighbors are where the two flows meet (async boundaries like job queues commonly sit between them)"
    except Exception:
        logger.debug("non-fatal error swallowed", exc_info=True)
    return hints


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


def traverse_graph(symbol_id: str, depth: int = 1, summary_only: bool = False, exclude_shared_state_types: bool = True) -> dict[str, object]:
    """Traverse the dependency graph from a symbol up to N hops, returning neighborhoods for every reachable symbol. Use this to map the full impact radius of a change across the codebase. Provide symbol_id and optional depth (default 1, max 5). Set summary_only=true for a compact response with just counts per hop — use this when you only need the blast radius size, not the full symbol lists. exclude_shared_state_types=true (default, same as get_blast_radius) filters god-types (GlobalState-like, >50 type-references) from every hop — without it a depth-3 traversal reaches ~40% of the graph through shared-state types and stops meaning anything."""
    depth = max(1, min(depth, 5))

    if not graph_index.has_symbol(symbol_id):
        return {"error": f"Symbol '{symbol_id}' not found in the graph index.", "symbol_id": symbol_id, "hint": "Use search_symbols to find the correct symbol_id."}

    # summary_only only needs counts, not full neighborhoods — allow a much
    # larger traversal than the payload-protecting default cap.
    max_hoods = 1000 if summary_only else _MAX_TRAVERSE_NEIGHBORHOODS
    # God-type filter (same default as get_blast_radius): shared-state types
    # connect to half the graph through USES edges — without it a depth-3
    # traversal from close_tag reached 3,065 symbols (40% of the graph),
    # 2,248 arriving at hop 2 via used_by. Blocked nodes are not expanded,
    # so paths THROUGH god types don't silently leak symbols into the hops.
    blocked = _god_type_blocklist() if exclude_shared_state_types else set()
    neighborhoods = graph_index.traverse(symbol_id, depth=depth, max_neighborhoods=max_hoods, exclude=blocked)

    all_symbols: set[str] = set()
    capped_neighborhoods: list[dict] = []
    for n in neighborhoods[:_MAX_TRAVERSE_NEIGHBORHOODS]:
        nb = n.model_dump() if hasattr(n, "model_dump") else n
        if blocked:
            for k in ("upstream", "downstream", "used_by", "uses"):
                nb[k] = [s for s in nb.get(k, []) if s not in blocked]
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
    """Get current graph index statistics — total nodes and edges. Read-only, safe to call anytime. graph_nodes = real symbols + bare edge-target nodes (unresolved callees — scaffolding); real_symbols counts only extracted symbols (file summaries, module exports, and trivial constants excluded), and excluded_from_graph reconciles those against get_index_meta's total_symbols chunk count."""
    stats = dict(graph_index.get_stats())
    try:
        from app.core.index_store import index_metadata_store
        snapshot = index_metadata_store.load_snapshot()
        if snapshot:
            stats["total_symbols_all_chunks"] = len(snapshot.chunks)
            # Reconcile against REAL symbols only — bare edge-target nodes
            # would push the naive subtraction negative (smoke caught this).
            stats["excluded_from_graph"] = len(snapshot.chunks) - stats.get("real_symbols", stats.get("graph_nodes", len(snapshot.chunks)))
            stats["exclusion_note"] = "file summaries, module exports, trivial constants, keyword-named parser artifacts"
    except Exception:
        logger.debug("non-fatal error swallowed", exc_info=True)
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
    result: dict[str, object] = {
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

    # Co-change partners: files that ship with this one — review them together.
    try:
        from app.rag.ingestion.git_ingestor import get_co_change_pairs
        pairs = get_co_change_pairs(days)
        partners: dict[str, int] = {}
        for (a, b), n in pairs.items():
            if a == chunk.file_path:
                partners[b] = n
            elif b == chunk.file_path:
                partners[a] = n
        if partners:
            top = sorted(partners.items(), key=lambda kv: -kv[1])[:3]
            result["co_change_partners"] = [
                {"file_path": f, "shared_commits": n} for f, n in top
            ]
            result["co_change_note"] = (
                "these files changed together with this one — review them in the same pass"
            )
    except Exception:
        logger.debug("co-change partner lookup failed for %s", symbol_id, exc_info=True)

    return result


def find_hotspots(days: int = 30, limit: int = 15, min_churn: int = 3, module_prefix: str = "") -> dict[str, object]:
    """Rank files by combined risk: git churn (commits in last N days) x graph connectivity x missing test coverage. High churn + highly connected + untested = change hotspot — where regressions are most likely and review effort is best spent. Use this to prioritize review and test-writing. Read-only, safe to call anytime."""
    limit = max(1, min(limit, 50))
    days = max(1, min(days, 365))

    from app.core.query_cache import cache_get, cache_set

    cache_args = {"days": days, "limit": limit, "min_churn": min_churn, "module_prefix": module_prefix}
    cached = cache_get("hotspots", cache_args)
    if cached is not None:
        return cached

    try:
        from app.rag.ingestion.git_ingestor import get_file_churn
        churn = get_file_churn(days)
    except Exception as exc:
        return {"error": f"git history unavailable: {exc}"}

    from app.core.index_store import index_metadata_store
    snapshot = index_metadata_store.load_snapshot()
    if not snapshot:
        return {"error": "index snapshot unavailable — trigger a reindex"}

    # Accept crate NAMES as prefixes: 'au-client' translates to the symbol-id
    # prefix 'crates::au::client' via the crate graph — name-built filters
    # silently matched nothing before
    if module_prefix:
        try:
            from app.rag.ingestion.crate_graph import load_crate_graph

            for info in load_crate_graph().values():
                if module_prefix in (info.get("path"), info.get("path", "").removesuffix("/src")) or module_prefix == info.get("module_prefix"):
                    module_prefix = info["module_prefix"]
                    break
        except Exception:
            logger.debug("non-fatal error swallowed", exc_info=True)

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

    # Incident history: commits whose subject says fix/hotfix/revert are
    # proven incident markers — files that keep needing them are riskier
    # than churn alone suggests.
    fix_counts: dict[str, int] = {}
    try:
        from app.rag.ingestion.git_ingestor import get_recent_history
        import re as _re
        _FIX_RE = _re.compile(r"\b(fix|hotfix|revert)\b", _re.IGNORECASE)
        for commit in get_recent_history(days):
            if _FIX_RE.search(commit.get("subject") or ""):
                for f in commit.get("files") or []:
                    fix_counts[f] = fix_counts.get(f, 0) + 1
    except Exception:
        logger.debug("non-fatal error swallowed", exc_info=True)

    # Ownership concentration: churn funneled through one author is a
    # bus-factor signal on top of raw churn.
    ownership: dict[str, dict] = {}
    try:
        from app.rag.ingestion.git_ingestor import get_ownership
        ownership = get_ownership(days)
    except Exception:
        logger.debug("non-fatal error swallowed", exc_info=True)

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
        fix_commits = fix_counts.get(file_path, 0)
        own = ownership.get(file_path, {})
        top_author = own.get("top_author", "")
        top_author_share = own.get("top_author_share", 0.0)
        degree_factor = min(max_degree, 100) / 10
        # no floor: a zero-connection file must score zero — flooring at 1.0
        # made disconnected files rank as hotspots (mis/au/models.rs scored
        # 23.6 with zero connections, contradicting the method description)
        test_factor = 1.5 if anchor_tests == 0 else (1.25 if untested_ratio > 0.8 else 1.0)
        incident_factor = 1.0 + min(fix_commits, 10) * 0.05
        owner_factor = 1.1 if (top_author_share >= 0.8 and commit_count >= 5) else 1.0
        score = commit_count * max(degree_factor, 0.1) * test_factor * incident_factor * owner_factor
        hotspots.append({
            "file_path": file_path,
            "score": round(score, 1),
            "components": {
                "commits": commit_count,
                "fix_or_revert_commits": fix_commits,
                "top_symbol_connections": max_degree,
                "file_total_connections": file_degree,
                "untested_symbol_ratio": round(untested_ratio, 2),
                "top_author": top_author,
                "top_author_share": top_author_share,
                "bus_factor": top_author_share >= 0.8 and commit_count >= 5,
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
    payload = {
        "window_days": days,
        "min_churn": min_churn,
        "hotspot_count": len(hotspots),
        "hotspots": hotspots[:limit],
        "method": "score = commits x min(top-symbol connections,100)/10 x test penalty (1.5/1.25/1.0) x incident factor (1 + 0.05 x fix-or-revert commits, capped 1.5)",
    }
    cache_set("hotspots", cache_args, payload, ttl=4*3600, tags=["graph", "git", "tests"])
    return payload


def _sccs(edges: list[tuple[str, str]], min_size: int = 2) -> list[list[str]]:
    """Iterative Tarjan strongly connected components (no recursion limits)."""
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
    return sccs


def get_crate_graph(crate: str = "") -> dict[str, object]:
    """Workspace crate dependencies from Cargo.toml — path deps and workspace-inherited deps between crates, with dependents and crate-level cycles. Cross-crate changes are riskier: a crate with many internal dependents has a wider blast radius. Call without arguments for the full workspace summary with cycles; pass a crate name (e.g. 'common') for that crate's dependencies, dependents, and cycle membership. Read-only, safe to call anytime."""
    try:
        from app.rag.ingestion.crate_graph import load_crate_graph, crate_edges
        graph = load_crate_graph()
    except Exception as exc:
        return {"error": f"crate graph unavailable: {exc}"}

    if not graph:
        return {"error": "no workspace members found — is this a Cargo workspace?"}

    edges = crate_edges(graph)


    if crate:
        crate = crate.strip()
        info = graph.get(crate)
        if info is None:
            return {"error": f"unknown crate: {crate}", "known_crates": sorted(graph.keys())[:30]}
        cycle_groups = [c for c in _sccs(edges) if crate in c]
        return {
            "crate": crate,
            "path": info["path"],
            "dependencies": info["dependencies"],
            "dependents": info["dependents"],
            "dependent_count": len(info["dependents"]),
            "cross_crate_risk": (
                "high" if len(info["dependents"]) > 5
                else "medium" if info["dependents"]
                else "leaf — no internal consumers"
            ),
            "cycle_membership": cycle_groups or None,
        }

    cycles = _sccs(edges)
    ranked = sorted(
        ((name, len(info["dependents"])) for name, info in graph.items()),
        key=lambda kv: -kv[1],
    )[:10]
    return {
        "crate_count": len(graph),
        "internal_edges": len(edges),
        "most_depended_on": [{"crate": n, "dependents": d} for n, d in ranked],
        "crate_cycles": [{"crates": c, "size": len(c)} for c in cycles] or None,
        "cycle_note": (
            f"{len(cycles)} crate-level cycle group(s) — circular crate dependencies"
            if cycles else "no crate-level cycles — workspace is a DAG"
        ),
        "crates": {name: {"dependencies": info["dependencies"], "dependents": len(info["dependents"])}
                   for name, info in sorted(graph.items())},
    }


def find_cycles(min_size: int = 2, limit: int = 10) -> dict[str, object]:
    """Find dependency cycles between modules — strongly connected components in the module-level call/type graph. Circular dependencies make changes ripple unpredictably and block clean testing; use this to find refactoring targets. Read-only, safe to call anytime."""
    limit = max(1, min(limit, 30))

    edges = graph_index.get_module_edges()
    if not edges:
        return {"cycles": [], "cycle_count": 0, "note": "no cross-module edges in the graph"}

    sccs = _sccs(edges, min_size)

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
            logger.debug("non-fatal error swallowed", exc_info=True)

    if not results and fuzzy:
        try:
            from app.core.index_store import index_metadata_store
            snapshot = index_metadata_store.load_snapshot()
            if snapshot:
                query_lower = query.lower()
                seen = set()
                for chunk in snapshot.chunks:
                    sid = chunk.symbol_id
                    if sid in seen or _is_noise_symbol(sid):
                        continue
                    if chunk.kind in ("module", "file_summary", "module_exports"):
                        continue  # pseudo-chunks outranked real symbols on filename queries
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
                # typo tier last: 'wallte' never corrected to 'wallet' —
                # substring fuzzy can't see transpositions
                if len(results) < limit:
                    import difflib as _dl
                    shorts = {}
                    for chunk in snapshot.chunks:
                        if not _is_noise_symbol(chunk.symbol_id) and chunk.kind not in ("module", "file_summary", "module_exports"):
                            shorts.setdefault(chunk.symbol_id.split("::")[-1].lower(), chunk.symbol_id)
                    for match in _dl.get_close_matches(query_lower, list(shorts), n=limit, cutoff=0.8):
                        sid = shorts[match]
                        if sid not in seen:
                            seen.add(sid)
                            results.append({
                                "symbol_id": sid,
                                "short_name": sid.split("::")[-1],
                                "match_type": "typo_correction",
                            })
        except Exception:
            logger.debug("non-fatal error swallowed", exc_info=True)

    if search_files:
        try:
            from app.core.index_store import index_metadata_store
            snapshot = index_metadata_store.load_snapshot()
            if snapshot:
                query_lower = query.lower().replace(".rs", "")
                existing_sids = {r["symbol_id"] for r in results}
                for chunk in snapshot.chunks:
                    if chunk.symbol_id in existing_sids or _is_noise_symbol(chunk.symbol_id):
                        continue
                    if chunk.kind in ("module", "file_summary", "module_exports"):
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
            logger.debug("non-fatal error swallowed", exc_info=True)

    # a filename-looking query wants the file's symbols first — fuzzy name
    # hits (module names, artifacts) ranked above file matches before
    looks_like_file = query.strip().endswith(".rs") or "/" in query
    if looks_like_file:
        results.sort(key=lambda r: 0 if r.get("match_type") == "file_path" else 1)

    return {
        "query": query,
        "matches": len(results),
        "symbols": results,
    }


# ============================================================================
# Idea 4: Semantic search with timeout + fallback
# ============================================================================

def _rerank_via_sidecar(query: str, documents: list[str], timeout: float = 4.0) -> dict | None:
    """Ask the cross-encoder sidecar to score query-doc relevance. Returns
    {"order": [...], ...} or None — never raises into the search path."""
    try:
        import requests

        url = getattr(_settings, "reranker_url", "") or "http://reranker:8001"
        r = requests.post(
            f"{url}/rerank",
            json={"query": query, "documents": documents},
            timeout=timeout,
        )
        if r.status_code == 200:
            data = r.json()
            return data
    except Exception:
        logger.debug("non-fatal error swallowed", exc_info=True)
    return None


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

    # Cross-encoder rerank: reorder the top hits by direct query-doc
    # relevance. Strict budget — the CPU box cannot let this dominate
    # latency; any failure keeps the hybrid ordering.
    rerank_info: dict[str, object] | None = None
    if reranked and getattr(_settings, "reranker_enabled", False):
        try:
            import concurrent.futures as _fut

            docs = [f"{m.symbol_id}\n{m.content[:400]}" for m, _, _ in reranked[:8]]
            with _fut.ThreadPoolExecutor(max_workers=1) as _ex:
                _rr = _ex.submit(
                    _rerank_via_sidecar, query, docs, timeout=3.5
                )
                rerank_info = _rr.result(timeout=4.0)
            if rerank_info and rerank_info.get("reranked_order"):
                order = rerank_info["reranked_order"][: len(reranked)]
                reranked = [reranked[i] for i in order if i < len(reranked)]
                rerank_info = {k: v for k, v in rerank_info.items() if k != "reranked_order"}
        except Exception:
            rerank_info = None

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
        logger.debug("non-fatal error swallowed", exc_info=True)

    try:
        from app.rag.reinforcement.feedback_store import record_query_pattern
        record_query_pattern(query, [m.symbol_id for m, _, _ in reranked if boost > 0])
    except Exception:
        logger.debug("non-fatal error swallowed", exc_info=True)

    SEMANTIC_SEARCH_LATENCY_MS.observe((_t.monotonic() - _search_start) * 1000)
    # Low-relevance signal: the TOP hit sitting at the 0.5 fallback floor
    # means the hybrid search found nothing real — agents trusted those hits
    # (a nonsense query returned "matches" headed by a floor-scored entry).
    # Top-only, not all-top-5: real results under a floor-scored leader are
    # still noise-ranked, but mixed lists with a genuine leader are fine.
    low_relevance = bool(reranked) and 0.49 <= reranked[0][1] <= 0.51
    response = _attach_stale_warning({
        "query": query,
        "query_id": query_id,
        "matches": len(reranked),
        "expansion_applied": bool(expansions),
        "reranked": rerank_info,
        "timed_out": False,
        "low_relevance": low_relevance,
        "low_relevance_note": (
            "all top scores sit at the scoring floor (~0.5) — the index likely has nothing "
            "for this query; treat these results as unranked filler, not matches"
        ) if low_relevance else None,
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
    return response


# ============================================================================
# Idea 6: MCP tool chaining — analyze_pr_diff
# ============================================================================

def analyze_pr_diff(diff_text: str, max_symbols: int = 10, pr_mode: bool = True) -> dict[str, object]:
    """Analyze a git diff and return changed symbols with blast radius in one call. This is a chained tool that: (1) parses the diff for function/struct/trait names, (2) resolves them against the graph, (3) gets blast radius for each resolved symbol, (4) returns a combined impact report. PR mode (default) classifies every symbol as removed/added/renamed/modified: removed and renamed symbols are resolved against the master index (renames report old_symbol_id + renamed_to so you query callers of the OLD name), added symbols are listed as 'not in index'. Use this instead of calling search_symbols + get_blast_radius separately. Provide the full git diff text."""
    from app.rag.diff_parser import resolve_diff_symbols

    resolved = resolve_diff_symbols(diff_text, graph_index)

    renamed = resolved.get("renamed_symbols", [])
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

    # Renamed symbols: the blast radius lives on the OLD id — that's what
    # callers still reference until this PR merges.
    renamed_blast: list[dict] = []
    for sym in renamed[:max_symbols]:
        sid = sym.get("symbol_id", "")
        if not sid:
            renamed_blast.append({**sym, "risk_score": "unknown"})
            continue
        br = get_blast_radius(sid)
        renamed_blast.append({
            **sym,
            "risk_score": br.get("risk_score", "unknown"),
            "total_connections": br.get("total_connections", 0),
            "upstream_count": br.get("total_upstream", 0),
        })

    pr_breakdown = None
    if pr_mode:
        pr_breakdown = {
            "removed_from_master": [
                {"symbol_id": s.get("symbol_id"), "short_name": s.get("short_name"),
                 "upstream_count": _upstream_count_for(s.get("symbol_id", ""))}
                for s in resolved.get("deleted_symbols", [])
            ],
            "renamed": [
                {"old_symbol_id": s.get("symbol_id"), "renamed_to": s.get("renamed_to"),
                 "found_in_master": s.get("found_in_graph", False),
                 "upstream_count": _upstream_count_for(s.get("symbol_id", ""))}
                for s in renamed
            ],
            "added_not_in_index": [
                {"short_name": s.get("short_name"), "note": "new symbol — no callers in master"}
                for s in resolved.get("new_symbols", [])
            ],
            "modified_in_master": [
                {"symbol_id": r.get("symbol_id"), "short_name": r.get("short_name"),
                 "change_type": r.get("change_type")}
                for r in blast_radius_results
            ],
        }

    return _attach_stale_warning({
        "changed_files": resolved["changed_files"],
        "resolved_symbols": blast_radius_results,
        "renamed_symbols": renamed_blast,
        "new_symbols_not_in_index": resolved["new_symbols"],
        "deleted_symbols": resolved["deleted_symbols"],
        "pr_breakdown": pr_breakdown,
        "summary": resolved["summary"],
        "high_risk_symbols": [r for r in blast_radius_results if r.get("risk_score") in ("high", "critical")],
        "change_type_breakdown": resolved.get("summary", {}).get("change_type_breakdown", {}),
    })


def _upstream_count_for(symbol_id: str) -> int:
    if not symbol_id:
        return 0
    br = get_blast_radius(symbol_id, summary_only=True)
    return int(br.get("total_upstream", 0)) if "error" not in br else 0


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

    try:
        from app.rag.retrieval.graph import graph_index as _gi

        provenance = _gi.get_edge_provenance(symbol_id)
    except Exception:
        provenance = {}

    for sym in br.get("upstream", []):
        detailed["upstream"].append({"symbol_id": sym, "confidence": "high", "edge_type": "calls", "via": provenance.get(sym, "regex")})
    for sym in br.get("downstream", []):
        detailed["downstream"].append({"symbol_id": sym, "confidence": "high", "edge_type": "calls", "via": provenance.get(sym, "regex")})
    for sym in br.get("used_by", []):
        modes = used_by_modes.get(sym, ["reference"])
        detailed["used_by"].append({"symbol_id": sym, "confidence": "medium", "edge_type": "type_reference", "usage_modes": modes, "via": provenance.get(sym, "regex")})
    for sym in br.get("uses", []):
        modes = uses_modes.get(sym, ["reference"])
        detailed["uses"].append({"symbol_id": sym, "confidence": "medium", "edge_type": "type_reference", "usage_modes": modes, "via": provenance.get(sym, "regex")})

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

# single source of truth for the live embedding model — hardcoded copies in
# docs/tools drift (the README bge/MiniLM contradiction came from exactly that)
try:
    from app.rag.retrieval.weaviate_semantic import _EMBED_DIMENSIONS, _EMBED_MODEL_NAME

    _EMBED_MODEL_REF = (_EMBED_MODEL_NAME, _EMBED_DIMENSIONS)
except Exception:  # store-less contexts (tests) keep a sane default
    _EMBED_MODEL_REF = ("unknown", 0)

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


def compute_coverage(repository_path: str, chunks: list) -> dict[str, object]:
    """Coverage: set difference over one universe of file paths (all .rs
    files, tests included). The old check excluded tests from `disk` but
    counted test + .sql paths in `files_indexed` — different universes, so
    files_indexed (795) > rust_files_on_disk (783) was possible and missing
    test files stayed invisible (missing_count: 0 while
    low_balance_core_tests.rs had 0 symbols)."""
    from pathlib import Path

    from app.rag.ingestion.tree_sitter import is_test_file_path

    repo = Path(repository_path)
    skip_dirs = {".git", "target", "node_modules", "__pycache__"}
    disk = {
        p.relative_to(repo).as_posix()
        for p in repo.rglob("*.rs")
        if not any(part in skip_dirs for part in p.relative_to(repo).parts)
    }
    indexed_rs = {c.file_path for c in chunks if c.file_path.endswith(".rs")}
    missing = sorted(disk - indexed_rs)
    symbol_counts: dict[str, int] = {}
    for c in chunks:
        if c.file_path.endswith(".rs") and c.kind not in ("file_summary", "module_exports"):
            symbol_counts[c.file_path] = symbol_counts.get(c.file_path, 0) + 1
    test_files_zero_symbols = sorted(
        f for f in disk
        if is_test_file_path(f) and symbol_counts.get(f, 0) == 0
    )
    return {
        "rust_files_on_disk": len(disk),
        "files_indexed": len(indexed_rs),
        "missing_from_index": missing[:20],
        "missing_count": len(missing),
        "test_files_zero_symbols": test_files_zero_symbols[:20],
        "test_files_zero_symbols_count": len(test_files_zero_symbols),
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
            logger.debug("non-fatal error swallowed", exc_info=True)

    # Coverage: files on disk vs indexed — one path universe, tests included.
    coverage = None
    if snapshot and snapshot.repository_path:
        try:
            coverage = compute_coverage(snapshot.repository_path, snapshot.chunks)
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
            logger.debug("non-fatal error swallowed", exc_info=True)

    # Symbol-set fingerprint: the commit pointer alone claimed freshness while
    # deleted symbols lingered from another revision. Three-way agreement —
    # stored hash (end of ingest), snapshot rows, live graph — makes "up to
    # date" mean the symbol set really matches.
    from app.schemas.codebase import compute_symbol_set_hash

    snapshot_hash = compute_symbol_set_hash(snapshot.chunks) if snapshot else ""
    stored_hash = (snapshot.symbol_set_hash if snapshot else "") or ""
    graph_hash = ""
    graph_symbol_count = None
    try:
        graph_ids = graph_index.get_symbol_ids()
        graph_symbol_count = len(graph_ids)
        graph_hash = compute_symbol_set_hash(graph_ids)
    except Exception:
        logger.debug("graph symbol-set hash unavailable", exc_info=True)

    hashes_agree = bool(snapshot_hash) and snapshot_hash == graph_hash
    # The stored ingest-time hash is advisory only: it used a different
    # eligibility formula in older builds and would veto freshness forever
    # after any formula change. Live graph-vs-snapshot is the drift detector.
    stored_hash_stale = bool(stored_hash and snapshot_hash and stored_hash != snapshot_hash)
    commit_fresh = bool(last_commit) and head != "unknown" and last_commit == head
    symbol_set_drift = None
    if snapshot_hash and graph_hash and snapshot_hash != graph_hash:
        from app.schemas.codebase import is_graph_eligible

        symbol_set_drift = {
            "snapshot_symbols": len({c.symbol_id for c in snapshot.chunks if is_graph_eligible(c)}) if snapshot else 0,
            "graph_symbols": graph_symbol_count,
            "hint": "graph and snapshot disagree — stale symbols on one side; run POST /api/index/rebuild to resync",
        }

    return {
        "graph_gen": gen,
        "classifier_version": _CLASSIFIER_VERSION,
        "last_indexed_commit": last_commit[:12],
        "current_head": head[:12],
        "commits_behind": commits_behind,
        "up_to_date": commit_fresh and hashes_agree,
        "up_to_date_detail": {
            "commit_matches_head": commit_fresh,
            "symbol_sets_match": hashes_agree,
            "symbol_set_hash": (stored_hash or snapshot_hash)[:16],
            "stored_hash_stale": stored_hash_stale,
            "drift": symbol_set_drift,
        },
        "snapshot_created_at": snapshot.created_at.isoformat() if snapshot and snapshot.created_at else None,
        "files_indexed": snapshot.files_indexed if snapshot else 0,
        "total_symbols": len(snapshot.chunks) if snapshot else 0,
        "total_edges": len(snapshot.graph_edges) if snapshot else 0,
        "weaviate_collection": weaviate_collection,
        "embedding_model": _EMBED_MODEL_REF[0],
        "coverage": coverage,
        "embedding_dimensions": _EMBED_MODEL_REF[1],
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

    # strip './' prefixes — Rust panic output uses them, and the exact-match
    # against indexed paths (never ./-prefixed) silently failed (regression
    # from the path-strictness fix)
    frames = [
        (f.removeprefix("./").removeprefix("/"), ln)
        for f, ln in _re.findall(r"([A-Za-z0-9_./-]+\.rs):(\d+)", stacktrace)
    ]
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
            # tolerate path prefix differences (leading crates/, src/) — but
            # only on full path-segment boundaries: a bare endswith made a
            # nonexistent 'tag_generation.rs' resolve into
            # process_tag_generation_file.rs (a fabricated frame match)
            suffix = file_path.rsplit("/", 2)[-1] if "/" in file_path else None
            if suffix and "/" in suffix:
                candidates = [
                    c for c in snapshot.chunks
                    if c.file_path.endswith("/" + suffix) and c.start_line <= line <= c.end_line
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
                # boilerplate exclusion: std trait impls (From, Display,
                # Serialize...) and per-module helper clones appear in every
                # module and dominated the drift score without meaning
                # anything — a 0.70 "drift" was 20x ist_day_start_utc +
                # From/from impls. Case-insensitive: Rust impls are TitleCase.
                short = sid.split("::")[-1]
                if short.lower() in _BOILERPLATE_SYMBOL_NAMES:
                    continue
                out.setdefault(short, []).append(sid)
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
        "boilerplate_excluded": sorted(_BOILERPLATE_SYMBOL_NAMES),
        "note": "only_in_a / only_in_b are the drift — counterparts missing from one side; std trait impls and common helper names are excluded from the score",
    }


def find_warnings_in_blast_radius(symbol_id: str, radius: int = 2, kinds: list[str] | None = None) -> dict[str, object]:
    """Find code warnings (TODO/FIXME/HACK comments, swallowed errors, stub functions, commented-out code, fail-open defaults) in a symbol's blast radius. This is the metal detector: bugs live in comments, dead code, and discarded Results — none visible in the dependency graph. Use during PR review or incident triage on hotspots. Detector kinds include 'dropped_future' (let _ = …await — result discarded), 'error_mask' (map_err(|_e| …) — the error is erased), and 'constant_return' (decision fns that always return the same value, like should_deduct_for_unsettled_transaction -> true). Provide the full symbol_id. Optional: radius (default 2, max 3 — how many dependency hops to include), kinds=['todo','fixme','hack','xxx','commented_code','error_swallow','fail_open','stub_fn','unsafe_block','ffi_boundary','panic_path','unwrap_density','dropped_future','error_mask','constant_return'] to filter."""
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
