"""Diff-aware symbol extraction — parse git diffs to find changed symbols.

Takes a unified git diff and extracts:
- Changed file paths
- Added/modified/deleted function and struct names
- Classifies change types (new, signature change, body-only, trait impl, struct field)
- Matches them against the graph index to resolve full symbol_ids
- Returns categorized lists: changed_symbols, new_symbols, deleted_symbols
"""

import re
import logging

logger = logging.getLogger(__name__)

_DIFF_FILE_PATTERN = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
_DIFF_HUNK_PATTERN = re.compile(r"^@@.*@@(.*)$", re.MULTILINE)

_RUST_FN_PATTERN = re.compile(
    r"^\s*(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
_RUST_STRUCT_PATTERN = re.compile(
    r"^\s*(?:pub\s+)?struct\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
_RUST_ENUM_PATTERN = re.compile(
    r"^\s*(?:pub\s+)?enum\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
_RUST_TRAIT_PATTERN = re.compile(
    r"^\s*(?:pub\s+)?trait\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
_RUST_IMPL_PATTERN = re.compile(
    r"^\s*impl(?:<[^>]+>)?\s+([A-Za-z_][A-Za-z0-9_<>\s,]*)\s*(?:for\s+([A-Za-z_][A-Za-z0-9_<>\s,]*))?\s*\{",
    re.MULTILINE,
)
_RUST_FN_SIGNATURE_PATTERN = re.compile(
    r"^\s*(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:<[^>]*>)?\s*\([^)]*\)",
    re.MULTILINE,
)
_RUST_STRUCT_FIELD_PATTERN = re.compile(
    r"^\s*(?:pub\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([A-Za-z_][A-Za-z0-9_<>&\s,]*)",
    re.MULTILINE,
)
_RUST_TRAIT_IMPL_PATTERN = re.compile(
    r"^\s*impl(?:<[^>]+>)?\s+([A-Za-z_][A-Za-z0-9_<>\s,]*)\s+for\s+([A-Za-z_][A-Za-z0-9_<>\s,]*)",
    re.MULTILINE,
)

_ADDED_LINE = re.compile(r"^\+(?!\+)")
_DELETED_LINE = re.compile(r"^-(?!-)")


def _classify_change(symbol_name: str, added_lines: list[str], deleted_lines: list[str], symbol_type: str) -> dict[str, object]:
    """Classify what kind of change happened to a symbol.

    Returns:
        {
            "change_type": "new" | "signature_change" | "body_only" | "trait_impl" | "struct_field" | "deleted" | "modified",
            "symbol_type": "function" | "struct" | "enum" | "trait" | "impl",
            "details": str,
        }
    """
    added_sig = any(_RUST_FN_SIGNATURE_PATTERN.search(l) and symbol_name in l for l in added_lines)
    deleted_sig = any(_RUST_FN_SIGNATURE_PATTERN.search(l) and symbol_name in l for l in deleted_lines)

    added_decl = any(
        p.search(l) and symbol_name in l
        for l in added_lines
        for p in [_RUST_FN_PATTERN, _RUST_STRUCT_PATTERN, _RUST_ENUM_PATTERN, _RUST_TRAIT_PATTERN]
    )
    deleted_decl = any(
        p.search(l) and symbol_name in l
        for l in deleted_lines
        for p in [_RUST_FN_PATTERN, _RUST_STRUCT_PATTERN, _RUST_ENUM_PATTERN, _RUST_TRAIT_PATTERN]
    )

    trait_impl_added = any(_RUST_TRAIT_IMPL_PATTERN.search(l) for l in added_lines)
    trait_impl_deleted = any(_RUST_TRAIT_IMPL_PATTERN.search(l) for l in deleted_lines)

    if trait_impl_added or trait_impl_deleted:
        impl_match = None
        for l in added_lines + deleted_lines:
            m = _RUST_TRAIT_IMPL_PATTERN.search(l)
            if m:
                impl_match = m
                break
        trait_name = impl_match.group(1).strip() if impl_match else "unknown"
        type_name = impl_match.group(2).strip() if impl_match and impl_match.lastindex >= 2 else "unknown"
        return {
            "change_type": "trait_impl",
            "symbol_type": "impl",
            "details": f"Trait impl {trait_name} for {type_name} modified — all consumers of this trait get the new behavior transitively",
        }

    if added_decl and not deleted_decl:
        return {
            "change_type": "new",
            "symbol_type": symbol_type,
            "details": f"New {symbol_type} '{symbol_name}' added",
        }

    if deleted_decl and not added_decl:
        return {
            "change_type": "deleted",
            "symbol_type": symbol_type,
            "details": f"{symbol_type.capitalize()} '{symbol_name}' removed",
        }

    if added_sig and deleted_sig:
        added_sigs = [_RUST_FN_SIGNATURE_PATTERN.search(l).group(0) for l in added_lines if _RUST_FN_SIGNATURE_PATTERN.search(l) and symbol_name in l]
        deleted_sigs = [_RUST_FN_SIGNATURE_PATTERN.search(l).group(0) for l in deleted_lines if _RUST_FN_SIGNATURE_PATTERN.search(l) and symbol_name in l]
        sig_changed = added_sigs != deleted_sigs
        if sig_changed:
            return {
                "change_type": "signature_change",
                "symbol_type": symbol_type,
                "details": f"Function signature changed — callers may need updating. Old: {deleted_sigs[0][:100] if deleted_sigs else '?'} → New: {added_sigs[0][:100] if added_sigs else '?'}",
            }

    if symbol_type == "struct":
        added_fields = [m.group(1) for l in added_lines for m in [_RUST_STRUCT_FIELD_PATTERN.search(l)] if m and symbol_name not in l]
        deleted_fields = [m.group(1) for l in deleted_lines for m in [_RUST_STRUCT_FIELD_PATTERN.search(l)] if m and symbol_name not in l]
        if added_fields or deleted_fields:
            parts = []
            if added_fields:
                parts.append(f"added fields: {', '.join(added_fields[:5])}")
            if deleted_fields:
                parts.append(f"removed fields: {', '.join(deleted_fields[:5])}")
            return {
                "change_type": "struct_field",
                "symbol_type": "struct",
                "details": f"Struct fields changed — {'; '.join(parts)}",
            }

    return {
        "change_type": "body_only",
        "symbol_type": symbol_type,
        "details": f"{symbol_type.capitalize()} '{symbol_name}' body modified (no signature change)",
    }


def extract_symbols_from_diff(diff_text: str) -> dict[str, object]:
    """Parse a unified git diff and extract symbol names with change classification.

    Returns:
        {
            "changed_files": ["src/foo.rs", ...],
            "added_symbols": ["new_function", "NewStruct", ...],
            "modified_symbols": ["existing_fn", ...],
            "deleted_symbols": ["removed_fn", ...],
            "change_details": {symbol_name: {change_type, symbol_type, details}, ...},
        }
    """
    changed_files = _DIFF_FILE_PATTERN.findall(diff_text)

    added_symbols: set[str] = set()
    deleted_symbols: set[str] = set()
    modified_symbols: set[str] = set()

    lines = diff_text.split("\n")
    current_file = None
    in_hunk = False

    added_lines_by_file: dict[str, list[str]] = {}
    deleted_lines_by_file: dict[str, list[str]] = {}

    for line in lines:
        if line.startswith("+++ b/"):
            current_file = line[6:]
            in_hunk = False
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if current_file and not current_file.endswith(".rs"):
            continue

        if _ADDED_LINE.match(line):
            content = line[1:]
            added_lines_by_file.setdefault(current_file, []).append(content)
            for pattern in [_RUST_FN_PATTERN, _RUST_STRUCT_PATTERN, _RUST_ENUM_PATTERN, _RUST_TRAIT_PATTERN]:
                for match in pattern.finditer(content):
                    name = match.group(1).strip()
                    added_symbols.add(name)

        elif _DELETED_LINE.match(line):
            content = line[1:]
            deleted_lines_by_file.setdefault(current_file, []).append(content)
            for pattern in [_RUST_FN_PATTERN, _RUST_STRUCT_PATTERN, _RUST_ENUM_PATTERN, _RUST_TRAIT_PATTERN]:
                for match in pattern.finditer(content):
                    name = match.group(1).strip()
                    deleted_symbols.add(name)

    modified_symbols = added_symbols & deleted_symbols
    added_symbols -= modified_symbols
    deleted_symbols -= modified_symbols

    all_added = added_symbols | modified_symbols
    all_added_lines: list[str] = []
    all_deleted_lines: list[str] = []
    for flist in added_lines_by_file.values():
        all_added_lines.extend(flist)
    for flist in deleted_lines_by_file.values():
        all_deleted_lines.extend(flist)

    change_details: dict[str, dict] = {}
    for name in all_added:
        symbol_type = "function"
        for l in all_added_lines + all_deleted_lines:
            if _RUST_STRUCT_PATTERN.search(l) and name in l:
                symbol_type = "struct"
                break
            elif _RUST_ENUM_PATTERN.search(l) and name in l:
                symbol_type = "enum"
                break
            elif _RUST_TRAIT_PATTERN.search(l) and name in l:
                symbol_type = "trait"
                break

        change_details[name] = _classify_change(name, all_added_lines, all_deleted_lines, symbol_type)

    for name in deleted_symbols:
        change_details[name] = {
            "change_type": "deleted",
            "symbol_type": "unknown",
            "details": f"Symbol '{name}' appears to be fully removed from the diff",
        }

    return {
        "changed_files": changed_files,
        "added_symbols": sorted(added_symbols),
        "modified_symbols": sorted(modified_symbols),
        "deleted_symbols": sorted(deleted_symbols),
        "change_details": change_details,
    }


def resolve_diff_symbols(diff_text: str, graph_index) -> dict[str, object]:
    """Parse diff and resolve symbols against the graph index.

    Returns categorized symbol_ids that exist in the graph vs new ones that don't.
    Each resolved symbol includes change classification details.
    """
    extraction = extract_symbols_from_diff(diff_text)

    changed_symbols: list[dict] = []
    new_symbols: list[dict] = []
    deleted_symbols: list[dict] = []

    all_names = set(extraction["added_symbols"] + extraction["modified_symbols"])
    change_details = extraction.get("change_details", {})

    for name in all_names:
        results = graph_index.search_symbols(name, limit=5)
        cd = change_details.get(name, {"change_type": "modified", "symbol_type": "function", "details": ""})
        if results:
            for r in results:
                changed_symbols.append({
                    "symbol_id": r["symbol_id"],
                    "short_name": name,
                    "status": "modified" if name in extraction["modified_symbols"] else "added_in_diff",
                    "found_in_graph": True,
                    "change_type": cd["change_type"],
                    "symbol_type": cd["symbol_type"],
                    "change_details": cd["details"],
                })
        else:
            new_symbols.append({
                "short_name": name,
                "status": "new" if name in extraction["added_symbols"] else "modified",
                "found_in_graph": False,
                "change_type": cd["change_type"],
                "symbol_type": cd["symbol_type"],
                "change_details": cd["details"],
                "note": "Symbol not in master index — likely a branch-only addition",
            })

    for name in extraction["deleted_symbols"]:
        results = graph_index.search_symbols(name, limit=3)
        cd = change_details.get(name, {"change_type": "deleted", "symbol_type": "unknown", "details": ""})
        for r in results:
            deleted_symbols.append({
                "symbol_id": r["symbol_id"],
                "short_name": name,
                "status": "deleted",
                "found_in_graph": True,
                "change_type": cd["change_type"],
                "symbol_type": cd["symbol_type"],
                "change_details": cd["details"],
            })

    return {
        "changed_files": extraction["changed_files"],
        "changed_symbols": changed_symbols,
        "new_symbols": new_symbols,
        "deleted_symbols": deleted_symbols,
        "summary": {
            "files_changed": len(extraction["changed_files"]),
            "symbols_resolved": len(changed_symbols),
            "new_symbols_not_in_index": len(new_symbols),
            "deleted_symbols": len(deleted_symbols),
            "change_type_breakdown": _count_change_types(changed_symbols + new_symbols),
        },
    }


def _count_change_types(symbols: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for s in symbols:
        ct = s.get("change_type", "unknown")
        counts[ct] = counts.get(ct, 0) + 1
    return counts
