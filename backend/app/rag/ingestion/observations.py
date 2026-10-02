"""Code observation extraction — the metal detector.

Bugs live in comments, dead code, and swallowed errors; none of those are
visible in a dependency graph. This module mines them from source during
indexing so agents can query warnings inside a symbol's blast radius.

Precision over recall: a missed warning is acceptable, a fabricated one
destroys trust. Every detector is deliberately conservative.
"""

import re
from dataclasses import dataclass, field

from app.schemas.codebase import CodeChunk

TODO_MARKERS = ("TODO", "FIXME", "HACK", "XXX")

_LINE_COMMENT = re.compile(r"^\s*//")
_TODO_LINE = re.compile(r"//\s*(TODO|FIXME|HACK|XXX)\b[:\s]*(.*)", re.IGNORECASE)
_LOG_STMT = re.compile(
    r"^\s*(println!|eprintln!|print!|dbg!|log::|tracing::|debug!|info!|warn!|error!|trace!)"
)
_ERROR_SWALLOW = re.compile(
    r"^\s*let\s+_\s*=\s*[A-Za-z_][\w:.]*\s*\(.*\)(\s*\.\s*await)*(\s*\.\s*[A-Za-z_][\w:.]*\s*\(.*\))*(\s*\.\s*await)*\s*;"
)
_FAIL_OPEN = re.compile(r"\.(unwrap_or_default|unwrap_or\(false\))\s*\(")
_AUTH_HINT = re.compile(r"auth|token|header|permission|secret|api_?key|signature|claim|session|role", re.IGNORECASE)
_UNSAFE_BLOCK = re.compile(r"^\s*unsafe\s*\{")
_EXTERN_FFI = re.compile(r"^\s*(?:pub\s+)?extern\s*[\"']?\w*[\"']?\s*(\(|\{|fn\b)")
_PANIC_PATH = re.compile(r"^\s*(panic!|unreachable!|todo!|unimplemented!)\s*[!(]")
_UNWRAP_EXPECT = re.compile(r"\.(unwrap|expect)\s*\(")
_CODE_IN_COMMENT = re.compile(
    r"(\bfn\s|\blet\s|\bmatch\s|\bimpl\s|\bstruct\s|\buse\s|\.await|\breturn\s|^\s*//\s*[\w:.]+\(.*\)\s*;|\}|\{)"
)
_ATTR_LINE = re.compile(r"^\s*#!?\[")
_BRACE_ONLY = re.compile(r"^\s*[{}()]*\s*$")
_STUB_MACROS = re.compile(r"^\s*(todo!|unimplemented!)\s*\(\s*\)\s*;?\s*$")


@dataclass
class Observation:
    file_path: str
    line: int
    kind: str
    detail: str
    symbol_id: str | None = field(default=None)


def extract_observations(source: str, file_path: str, chunks: list[CodeChunk]) -> list[Observation]:
    """Mine a single source file. `chunks` provide line ranges for symbol
    attachment and function bodies for stub detection."""
    lines = source.split("\n")
    out: list[Observation] = []

    for i, line in enumerate(lines, start=1):
        m = _TODO_LINE.search(line)
        if m:
            detail = (m.group(2) or "").strip() or line.strip()
            out.append(Observation(file_path, i, m.group(1).lower(), detail[:200]))
            continue
        if _ERROR_SWALLOW.match(line):
            out.append(Observation(file_path, i, "error_swallow", line.strip()[:200]))
        if _FAIL_OPEN.search(line) and _auth_context(lines, i):
            out.append(Observation(file_path, i, "fail_open", line.strip()[:200]))
        if _UNSAFE_BLOCK.match(line):
            out.append(Observation(file_path, i, "unsafe_block", line.strip()[:200]))
        if _EXTERN_FFI.match(line):
            out.append(Observation(file_path, i, "ffi_boundary", line.strip()[:200]))
        if _PANIC_PATH.match(line):
            out.append(Observation(file_path, i, "panic_path", line.strip()[:200]))

    out.extend(_commented_code_blocks(lines, file_path))
    out.extend(_block_comment_dead_code(source, file_path))
    out.extend(_stub_functions(chunks, file_path))
    out.extend(_unwrap_density(chunks, file_path))

    fn_chunks = sorted(
        (c for c in chunks if c.kind in ("fn", "method") and c.start_line and c.end_line),
        key=lambda c: c.start_line,
    )
    for o in out:
        o.symbol_id = _symbol_for_line(fn_chunks, o.line)
    return out


def _commented_code_blocks(lines: list[str], file_path: str) -> list[Observation]:
    """Runs of 3+ line-comments that look like disabled code."""
    out: list[Observation] = []
    i = 0
    while i < len(lines):
        if not _LINE_COMMENT.match(lines[i]):
            i += 1
            continue
        j = i
        while j < len(lines) and _LINE_COMMENT.match(lines[j]) and not _TODO_LINE.search(lines[j]):
            j += 1
        if j == i:
            i += 1  # TODO-marked comment line — skip it, it's handled above
            continue
        run = lines[i:j]
        if len(run) >= 3:
            codeish = sum(1 for ln in run if _CODE_IN_COMMENT.search(ln))
            if codeish >= 2 and codeish >= (len(run) + 1) // 2:
                detail = " ".join(ln.strip().lstrip("/").strip() for ln in run[:3])[:200]
                out.append(Observation(file_path, i + 1, "commented_code", detail))
        i = j
    return out


def _block_comment_dead_code(source: str, file_path: str) -> list[Observation]:
    """Old code left inside /* */ blocks — the disk-verified duplicate-fn
    case. A block comment containing fn/struct definitions is dead code
    pretending to be live."""
    from app.rag.ingestion.tree_sitter import _block_comment_spans

    out: list[Observation] = []
    for start, end in _block_comment_spans(source):
        text = source[start:end]
        if "/*" not in text or len(text) < 30:
            continue
        if _CODE_IN_COMMENT.search(text) and re.search(r"\bfn\s+\w+", text):
            line = source[:start].count("\n") + 1
            out.append(
                Observation(file_path, line, "commented_code",
                            f"dead code in /* */ block: {text.strip()[:150]}")
            )
    return out


def _stub_functions(chunks: list[CodeChunk], file_path: str) -> list[Observation]:
    """Functions whose body is only logging/print statements (or nothing)."""
    out: list[Observation] = []
    for chunk in chunks:
        if chunk.kind not in ("fn", "method"):
            continue
        body_lines: list[str] = []
        for ln in chunk.content.split("\n"):
            s = ln.strip()
            if not s or s.startswith("///") or s.startswith("//!") or s.startswith("//"):
                continue
            if _ATTR_LINE.match(ln) or _BRACE_ONLY.match(ln):
                continue
            if "fn " in s and ("{" in s or s.endswith("->")):
                continue  # signature line
            body_lines.append(s)
        if not body_lines:
            continue
        if all(_STUB_MACROS.match(s) for s in body_lines):
            out.append(Observation(file_path, chunk.start_line, "stub_fn", "body is todo!/unimplemented!()"))
            continue
        if all(_LOG_STMT.match(s) or _STUB_MACROS.match(s) for s in body_lines):
            out.append(
                Observation(file_path, chunk.start_line, "stub_fn",
                            f"body is only logging: {body_lines[0][:120]}")
            )
    return out


def _unwrap_density(chunks: list[CodeChunk], file_path: str) -> list[Observation]:
    """Functions with high unwrap()/expect() density — cheap to detect,
    correlates with incidents, and reviews want to know before touching one."""
    out: list[Observation] = []
    for chunk in chunks:
        if chunk.kind not in ("fn", "method"):
            continue
        count = len(_UNWRAP_EXPECT.findall(chunk.content))
        if count >= 5:
            out.append(
                Observation(file_path, chunk.start_line, "unwrap_density",
                            f"{count} unwrap()/expect() calls in one function")
            )
    return out


def _auth_context(lines: list[str], line_no: int) -> bool:
    """Auth hint on the same line or within the two preceding non-empty lines
    (covers multi-line match arms and builder chains)."""
    window = lines[max(0, line_no - 3) : line_no]
    return any(_AUTH_HINT.search(ln) for ln in window)


def _symbol_for_line(fn_chunks: list[CodeChunk], line: int) -> str | None:
    """Innermost enclosing function chunk for a line."""
    best: CodeChunk | None = None
    for c in fn_chunks:
        if c.start_line <= line <= c.end_line:
            if best is None or c.start_line >= best.start_line:
                best = c
        elif c.start_line > line:
            break
    return best.symbol_id if best else None
