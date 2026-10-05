"""SCIP index ingestion — precise, type-resolved edges from rust-analyzer.

The regex extractor approximates call/type edges from syntax; SCIP gives
rust-analyzer's actual resolution: every reference with its exact target.
Division of labor:

  - containment: our chunk LINE RANGES (rust-analyzer's enclosing_range is
    unpopulated; our ranges come from the same source text and are reliable
    for "which symbol's body is this reference in")
  - targets: SCIP's resolved symbol (what regex resolution gets wrong —
    trait dispatch, imports, cross-module names)

A reference to a fn/method target becomes a CALLS edge; a type target
becomes a USES edge. Files covered by SCIP use ONLY scip edges; files
absent from the index keep their regex edges.

Symbol id mapping: short names resolve same-file first, then globally
unique, then module-path suffix. Ambiguous names are skipped — guessing
would recreate the phantom-edge problem SCIP exists to fix.
"""

import logging
from collections import defaultdict

from app.rag.ingestion.scip import scip_pb2

logger = logging.getLogger(__name__)

_SCIP_RS_SUFFIX = ".rs"
# edge sources must be callable units — mods are containers, modules
# excluded from the graph anyway
_EDGE_SOURCE_KINDS = {"fn", "impl"}


def parse_index(path: str) -> scip_pb2.Index:
    index = scip_pb2.Index()
    with open(path, "rb") as f:
        index.ParseFromString(f.read())
    return index


def _range_start_line(range_array) -> int | None:
    """First line of a SCIP range (3-int single-line or 4-int multiline)."""
    try:
        vals = list(range_array)
        if vals and isinstance(vals[0], int):
            return vals[0]
    except Exception:
        pass
    return None


def _short_name(scip_symbol: str) -> str:
    """rust-analyzer symbol path is parts[4]:
    'product/controller/wallet_closure/get_list_wallet_closure().'
    Module paths end with '/', fn/method names with '().', assoc items with '.'.
    """
    parts = scip_symbol.split(" ", 4)
    path = parts[4] if len(parts) == 5 else scip_symbol
    path = path.rstrip(".")
    segs = [s for s in path.split("/") if s]
    if not segs:
        return ""
    name = segs[-1]
    if name.endswith("()"):
        name = name[:-2]
    return name


def build_scip_edges(scip_path: str, chunks) -> tuple[dict[str, list[tuple[str, str]]], dict]:
    """Map SCIP references onto chunk symbol ids.

    Returns (edges, stats):
      edges: {source_symbol_id: [(target_symbol_id, "calls"|"uses"), ...]}
      stats: counts for observability
    """
    index = parse_index(scip_path)

    # (file, short name) -> chunk; ambiguous in-file names are skipped
    by_file_name: dict[tuple[str, str], list] = defaultdict(list)
    chunk_kind: dict[str, str] = {}
    for chunk in chunks:
        if not chunk.file_path or not chunk.symbol_id:
            continue
        short = chunk.symbol_id.split("::")[-1]
        by_file_name[(chunk.file_path, short)].append(chunk)
        chunk_kind[chunk.symbol_id] = chunk.kind
    unambiguous = {k: v[0] for k, v in by_file_name.items() if len(v) == 1}

    # global short-name index for cross-file reference resolution
    name_index: dict[str, list[str]] = defaultdict(list)
    for chunk in chunks:
        if chunk.symbol_id:
            name_index[chunk.symbol_id.split("::")[-1]].append(chunk.symbol_id)

    # per-file body regions from chunk line ranges — the containment model
    regions_by_file: dict[str, list[tuple[int, int, str, str]]] = defaultdict(list)
    for chunk in chunks:
        if chunk.file_path and chunk.symbol_id and chunk.kind in _EDGE_SOURCE_KINDS:
            regions_by_file[chunk.file_path].append(
                (chunk.start_line, chunk.end_line, chunk.symbol_id, chunk.kind)
            )

    stats = {
        "documents": len(index.documents),
        "scip_symbols": sum(len(d.symbols) for d in index.documents),
        "refs_total": 0,
        "refs_in_source_bodies": 0,
        "targets_resolved": 0,
        "edges_calls": 0,
        "edges_uses": 0,
        "files_covered": 0,
    }

    edges: dict[str, list[tuple[str, str]]] = defaultdict(list)
    seen_edges: set[tuple[str, str, str]] = set()

    for document in index.documents:
        rel_path = document.relative_path
        if not rel_path.endswith(_SCIP_RS_SUFFIX):
            continue
        stats["files_covered"] += 1
        regions = regions_by_file.get(rel_path)
        if not regions:
            continue

        for occ in document.occurrences:
            if occ.symbol_roles & 1:
                continue  # definitions carry no edge information here
            stats["refs_total"] += 1
            line = _range_start_line(occ.range)
            if line is None:
                continue

            # innermost fn/impl region containing this reference's line
            containing = [
                r for r in regions if r[0] <= line <= r[1]
            ]
            if not containing:
                continue  # imports/module-level mentions — not fn bodies
            source = min(containing, key=lambda r: r[1] - r[0])
            source_id = source[2]
            stats["refs_in_source_bodies"] += 1

            target_id = _resolve_target(occ.symbol, rel_path, unambiguous, name_index)
            if target_id is None or target_id == source_id:
                continue
            stats["targets_resolved"] += 1

            relation = "calls" if chunk_kind.get(target_id, "") == "fn" else "uses"
            key = (source_id, target_id, relation)
            if key not in seen_edges:
                seen_edges.add(key)
                edges[source_id].append((target_id, relation))
                stats["edges_calls" if relation == "calls" else "edges_uses"] += 1

    return dict(edges), stats


def _resolve_target(scip_symbol: str, rel_path: str, unambiguous: dict, name_index: dict) -> str | None:
    """Resolve a referenced SCIP symbol to our symbol id.

    1. same file, unique short name (most references are local or imported)
    2. globally unique short name
    3. module-path suffix match (crate module path -> our '::' id suffix)
    Ambiguity is skipped rather than guessed.
    """
    short = _short_name(scip_symbol)
    if not short:
        return None
    local = unambiguous.get((rel_path, short))
    if local is not None:
        return local.symbol_id

    candidates = name_index.get(short) or []
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        return None

    parts = scip_symbol.split(" ", 4)
    if len(parts) == 5:
        path = parts[4].rstrip(".")
        segments = []
        for seg in path.split("/"):
            seg = seg.strip()
            if seg.endswith("()"):
                seg = seg[:-2]
            if seg:
                segments.append(seg)
        for take in range(len(segments), 1, -1):
            suffix = "::" + "::".join(segments[-take:])
            for cand in candidates:
                if cand.endswith(suffix):
                    return cand
    return None
