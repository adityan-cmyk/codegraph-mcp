import re
from pathlib import Path

from app.rag.ingestion.chunking import build_code_chunk
from app.schemas.codebase import CodeChunk


RUST_SYMBOL_PATTERN = re.compile(
    r"^(?P<doc>(?:///[^\n]*\n)*)"
    r"\s*(?P<attrs>(?:#\[[^\]]+\]\s*)*)"
    r"\s*(?:pub\s+)?(?:(?:async\s+|unsafe\s+|extern\s+\"[^\"]+\"\s+)*)?(?P<kind>fn|struct|enum|trait|impl|type|const|static|mod)\s+"
    r"(?P<name>[A-Za-z0-9_<>]+)"
    r"(?P<signature>[^{;]*)?",
    re.MULTILINE,
)

RUST_USE_PATTERN = re.compile(
    r"^(?:pub\s+)?use\s+(?P<path>[^;]+);",
    re.MULTILINE,
)

RUST_IMPL_PATTERN = re.compile(
    r"^(?P<doc>(?:///[^\n]*\n)*)"
    r"\s*(?P<attrs>(?:#\[[^\]]+\]\s*)*)"
    r"\s*(?:pub\s+)?impl\s+"
    r"(?P<name>[A-Za-z0-9_<>,\s:]+?)"
    r"(?:\s+for\s+(?P<target>[A-Za-z0-9_<>,\s:]+))?"
    r"\s*\{",
    re.MULTILINE,
)

RUST_FN_IN_IMPL_PATTERN = re.compile(
    r"^(?P<doc>(?:///[^\n]*\n)*)"
    r"\s*(?P<attrs>(?:#\[[^\]]+\]\s*)*)"
    r"\s*(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+"
    r"(?P<name>[A-Za-z0-9_]+)"
    r"(?P<signature>[^{;]*)",
    re.MULTILINE,
)

MIN_CHUNK_LINES = 3

TRIVIAL_CONST_PATTERN = re.compile(
    r"^\s*(?:pub\s+)?const\s+\w+\s*:\s*\S+\s*=\s*[^;]+;\s*$",
    re.MULTILINE,
)

TRIVIAL_MOD_PATTERN = re.compile(
    r"^\s*(?:pub\s+)?mod\s+\w+\s*;\s*$",
    re.MULTILINE,
)

DERIVE_TRAIT_NAMES = frozenset({
    "Serialize", "Deserialize", "Default", "Eq", "PartialEq",
    "Clone", "Copy", "Debug", "Display", "Hash",
    "PartialOrd", "Ord", "AsRef", "AsMut", "From",
    "Into", "TryFrom", "TryInto", "Borrow", "BorrowMut",
    "ToOwned", "FromStr", "IntoIterator",
})


def _is_trivial_constant(content: str) -> bool:
    stripped = content.strip()
    if len(stripped.splitlines()) <= 2 and TRIVIAL_CONST_PATTERN.match(stripped):
        return True
    if "Relation {}" in stripped or "Relation ()" in stripped:
        return True
    return False


def _extract_docstring(doc_lines: str) -> str:
    lines = [line.strip().removeprefix("///").strip() for line in doc_lines.strip().splitlines()]
    return " ".join(line for line in lines if line)


def _clean_signature(signature: str) -> str:
    return " ".join(signature.strip().split())


def _extract_use_statements(source: str) -> list[str]:
    return [m.group("path").strip() for m in RUST_USE_PATTERN.finditer(source)]


def _count_trivial_lines(source: str) -> int:
    return sum(1 for line in source.splitlines() if TRIVIAL_MOD_PATTERN.match(line))


def generate_symbol_id(module_path: str, symbol_name: str) -> str:
    parts = module_path.split("/")
    cleaned = [p for p in parts if p != "src"]
    normalized_module = "::".join(cleaned).strip(":")
    # Strip generic-parameter artifacts: the regex name group captures
    # 'foo<C' / 'foo<S>' for generic fns, which used to be mangled into
    # 'foo_C_' / 'foo_S_' symbol ids — monomorphization noise that broke
    # lookups and polluted the graph. Generic params are not part of a
    # symbol's identity for navigation.
    base_name = symbol_name.split("<")[0].strip().rstrip("_")
    if not base_name:
        base_name = symbol_name.replace("<", "_").replace(">", "_").replace(",", "_")
    return f"{normalized_module}::{base_name}"


def _infer_domain(file_path: str) -> str:
    parts = file_path.split("/")
    domain_keywords = {
        "db": "database", "database": "database", "sql": "database", "migration": "database",
        "http": "http", "handler": "http", "route": "http", "api": "http", "controller": "http",
        "auth": "auth", "otp": "auth", "login": "auth", "token": "auth",
        "redis": "cache", "cache": "cache",
        "config": "config", "settings": "config",
        "model": "model", "entity": "model", "schema": "model",
        "service": "service", "repository": "service",
        "test": "test", "tests": "test", "spec": "test",
    }
    for part in parts:
        if part in domain_keywords:
            return domain_keywords[part]
    return ""


def generate_file_summary_chunk(file_path: str, source: str, symbol_chunks: list[CodeChunk]) -> CodeChunk | None:
    if not symbol_chunks:
        return None
    use_stmts = _extract_use_statements(source)
    lines: list[str] = []
    module_id = file_path.removesuffix(".rs")
    lines.append(f"// File: {file_path}")
    lines.append(f"// Module: {module_id.replace('/', '::')}")
    lines.append(f"// Symbols defined: {len(symbol_chunks)}")
    if use_stmts:
        lines.append(f"// Imports: {', '.join(use_stmts[:30])}")
    lines.append("")
    for chunk in symbol_chunks:
        sig = chunk.content.split("{", 1)[0].strip() if "{" in chunk.content else chunk.content.split("\n", 1)[0].strip()
        doc_lines = []
        for line in chunk.content.splitlines():
            stripped = line.strip()
            if stripped.startswith("///"):
                doc_lines.append(stripped.removeprefix("///").strip())
            else:
                break
        doc_text = " ".join(doc_lines)
        if len(sig) > 120:
            sig = sig[:117] + "..."
        visibility = "pub " if chunk.content.lstrip().startswith("pub ") else ""
        entry = f"  {visibility}{chunk.kind} {chunk.symbol_id.split('::')[-1]}  // line {chunk.start_line}"
        if doc_text:
            entry += f"\n  // {doc_text[:200]}"
        if chunk.kind == "fn" and sig:
            entry += f"\n  // {sig[:150]}"
        lines.append(entry)
    summary_text = "\n".join(lines)
    return build_code_chunk(
        symbol_id=generate_symbol_id(module_id, "file_summary"),
        file_path=file_path,
        kind="file_summary",
        content=summary_text,
        start_line=1,
        end_line=source.count("\n") + 1,
    )


RUST_MOD_DECL_PATTERN = re.compile(
    r"^\s*(?:pub\s+)?mod\s+(?P<name>\w+)\s*;",
    re.MULTILINE,
)


def generate_module_exports_chunk(file_path: str, source: str) -> CodeChunk | None:
    basename = Path(file_path).name
    if basename not in ("mod.rs", "lib.rs"):
        return None
    mod_decls = [m.group("name") for m in RUST_MOD_DECL_PATTERN.finditer(source)]
    if not mod_decls:
        return None
    use_stmts = _extract_use_statements(source)
    module_id = file_path.removesuffix(".rs")
    lines: list[str] = []
    lines.append(f"// Module exports: {file_path}")
    lines.append(f"// Crate: {module_id.replace('/', '::')}")
    if use_stmts:
        lines.append(f"// Re-exports: {', '.join(use_stmts[:30])}")
    lines.append(f"// Sub-modules: {', '.join(mod_decls)}")
    lines.append("")
    lines.append(source.rstrip())
    content = "\n".join(lines)
    return build_code_chunk(
        symbol_id=generate_symbol_id(module_id, "module_exports"),
        file_path=file_path,
        kind="module_exports",
        content=content,
        start_line=1,
        end_line=source.count("\n") + 1,
    )


_TEST_ATTR_PATTERN = re.compile(r"#\[[^\]]*\btest\]")
_CFG_TEST_MOD_PATTERN = re.compile(r"#\[cfg\(test\)\]\s*(?:pub\s+)?mod\s+\w+")


def _cfg_test_spans(source: str) -> list[tuple[int, int]]:
    """Brace-counted spans of #[cfg(test)] modules — everything inside is a test."""
    spans: list[tuple[int, int]] = []
    for m in _CFG_TEST_MOD_PATTERN.finditer(source):
        brace = source.find("{", m.end())
        if brace < 0:
            continue
        depth = 0
        i = brace
        while i < len(source):
            c = source[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        spans.append((m.start(), min(i + 1, len(source))))
    return spans


def _is_test_offset(offset: int, source: str, match_text: str, cfg_spans: list[tuple[int, int]]) -> bool:
    """#[test]/#[tokio::test] attribute on the item, or any position inside a #[cfg(test)] module."""
    if _TEST_ATTR_PATTERN.search(match_text):
        return True
    preceding = source[max(0, offset - 150):offset]
    # don't let attrs from the previous item bleed across its closing brace
    cut = preceding.rfind("}")
    if cut >= 0:
        preceding = preceding[cut + 1:]
    if _TEST_ATTR_PATTERN.search(preceding):
        return True
    return any(start <= offset < end for start, end in cfg_spans)


def _extract_impl_methods(impl_match: re.Match, source: str, module_id: str, all_matches: list[re.Match], match_index: int, cfg_test_spans: list[tuple[int, int]] = None) -> list[CodeChunk]:
    """Extract individual methods from an impl block as separate chunks."""
    impl_name_raw = impl_match.group("name").strip()
    impl_symbol = impl_name_raw.split("<")[0].strip().split(" for ")[0].strip()

    start_offset = impl_match.start()
    end_offset = all_matches[match_index + 1].start() if match_index + 1 < len(all_matches) else len(source)
    impl_body = source[start_offset:end_offset]

    fn_matches = list(RUST_FN_IN_IMPL_PATTERN.finditer(impl_body))
    if not fn_matches:
        return []

    method_chunks: list[CodeChunk] = []
    for fi, fn_match in enumerate(fn_matches):
        fn_start = fn_match.start()
        fn_end = fn_matches[fi + 1].start() if fi + 1 < len(fn_matches) else len(impl_body)
        fn_content = impl_body[fn_start:fn_end]
        fn_name = fn_match.group("name")
        abs_fn_start = start_offset + fn_start
        fn_is_test = _is_test_offset(abs_fn_start, source, fn_content, cfg_test_spans or [])

        if len(fn_content.strip()) < MIN_CHUNK_LINES:
            continue

        abs_start_line = source[:start_offset + fn_start].count("\n") + 1
        abs_end_line = source[:start_offset + fn_end].count("\n") + 1

        doc = _extract_docstring(fn_match.group("doc") or "")

        prefix = ""
        if doc:
            prefix += f"// {doc}\n"
        method_content = prefix + fn_content if prefix else fn_content

        symbol_id = f"{module_id}::{impl_symbol}::{fn_name}"
        method_chunks.append(
            build_code_chunk(
                symbol_id=symbol_id,
                file_path="",
                kind="fn",
                content=method_content,
                start_line=abs_start_line,
                end_line=abs_end_line,
                is_test=fn_is_test,
            )
        )
    return method_chunks


def _block_comment_spans(source: str) -> list[tuple[int, int]]:
    """Spans of (nested) /* */ block comments, ignoring those inside strings.

    The regex extractor has no comment awareness — an old fn version left
    inside /* */ was indexed as a live symbol (disk-verified duplicate:
    is_eligible_for_post_dated_cb at both 211 and 238). This scanner gives
    the extractor the context it lacks.
    """
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(source)
    depth = 0
    span_start = -1
    in_string = False
    string_ch = ""
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == string_ch:
                in_string = False
            i += 1
            continue
        if depth == 0 and ch in ('"', "'"):
            in_string = True
            string_ch = ch
            i += 1
            continue
        if ch == "/" and nxt == "*":
            if depth == 0:
                span_start = i
            depth += 1
            i += 2
            continue
        if ch == "*" and nxt == "/" and depth > 0:
            depth -= 1
            if depth == 0:
                spans.append((span_start, i + 2))
            i += 2
            continue
        i += 1
    if depth > 0 and span_start >= 0:
        spans.append((span_start, n))
    return spans


def extract_rust_chunks(file_path: str, source: str) -> list[CodeChunk]:
    lines = source.splitlines()
    comment_spans = _block_comment_spans(source)
    cfg_test = _cfg_test_spans(source)
    matches = [
        m for m in RUST_SYMBOL_PATTERN.finditer(source)
        if not any(s <= m.start() < e for s, e in comment_spans)
    ]

    use_stmts = _extract_use_statements(source)
    use_block = ""
    if use_stmts:
        use_block = "// Imports: " + ", ".join(use_stmts[:20]) + "\n\n"

    if not matches:
        if len(lines) <= 3 and _count_trivial_lines(source) == len(source.strip().splitlines()):
            return []
        fallback_symbol = generate_symbol_id(file_path.removesuffix(".rs"), "module")
        module_content = use_block + source if use_block else source
        return [
            build_code_chunk(
                symbol_id=fallback_symbol,
                file_path=file_path,
                kind="module",
                content=module_content,
                start_line=1,
                end_line=max(len(lines), 1),
            )
        ]

    chunks: list[CodeChunk] = []
    module_id = file_path.removesuffix(".rs")

    for index, match in enumerate(matches):
        start_offset = match.start()
        end_offset = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        start_line = source[:start_offset].count("\n") + 1
        end_line = source[:end_offset].count("\n") + 1
        symbol_name = match.group("name")
        kind = match.group("kind")
        doc = _extract_docstring(match.group("doc") or "")

        chunk_content = source[start_offset:end_offset]
        if kind == "mod" and len(chunk_content.strip()) <= 30:
            continue
        if kind == "const" and _is_trivial_constant(chunk_content):
            continue
        if kind in ("struct", "enum") and len(chunk_content.strip()) <= 40:
            continue

        prefix = ""
        if doc:
            prefix += f"// {doc}\n"
        if use_block and index == 0:
            prefix += use_block
        if prefix:
            chunk_content = prefix + chunk_content

        symbol_id = generate_symbol_id(module_id, symbol_name)
        is_test = kind == "fn" and _is_test_offset(match.start(), source, match.group(0), cfg_test)
        chunks.append(
            build_code_chunk(
                symbol_id=symbol_id,
                file_path=file_path,
                kind=kind,
                content=chunk_content,
                start_line=start_line,
                end_line=end_line,
                is_test=is_test,
            )
        )

        if kind == "impl":
            method_chunks = _extract_impl_methods(match, source, module_id, matches, index, cfg_test_spans=cfg_test)
            for mc in method_chunks:
                mc.file_path = file_path
            chunks.extend(method_chunks)

    return chunks
