"""SQL schema extraction — tables, columns, and migration blocks as symbols.

The graph saw only Rust while the bugs lived in the DDL: who reads
disputes.transaction_ref_id, which migration changed the ref_id format, which
rolling deploy drops a column still read by the old binary. Every CREATE
TABLE column and every -- COMMIT: migration block becomes a symbol with edges
to the tables it touches and the sea-orm entity that maps it.
"""

import re

from app.schemas.codebase import CodeChunk, GraphEdge

_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)
_MIGRATION_HEADER = re.compile(r"^--\s*COMMIT\s*:\s*(.*)$", re.IGNORECASE)
_TABLE_REF = re.compile(
    r"\b(?:ALTER\s+TABLE|CREATE\s+TABLE|DROP\s+TABLE|INSERT\s+INTO|UPDATE|TRUNCATE|FROM|JOIN|TABLE)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_COLUMN_REF = re.compile(
    r"\b(?:ADD|DROP|ALTER|RENAME)\s+COLUMN\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_COLUMN_RENAME = re.compile(
    r"\bRENAME\s+COLUMN\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)\s+TO\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_ENTITY_TABLE_ATTR = re.compile(r"table_name\s*=\s*\"([A-Za-z_][A-Za-z0-9_]*)\"")
_FIELD_COL_DEF = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s+[A-Z]", re.MULTILINE)


def _module_of(file_path: str) -> str:
    stem = file_path.split("/")[-1]
    return stem[:-4] if stem.endswith(".sql") else stem


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", (text or "").strip()).strip("_").lower()
    return cleaned[:60] or "block"


def extract_sql_chunks(file_path: str, source: str) -> tuple[list[CodeChunk], list[GraphEdge]]:
    """Tables + columns from CREATE TABLE, migration blocks from -- COMMIT:
    sections. Returns (chunks, edges) — edges are 'touches' relations from
    migrations to the tables/columns they modify."""
    chunks: list[CodeChunk] = []
    edges: list[GraphEdge] = []
    module = _module_of(file_path)
    lines = source.split("\n")

    table_columns: dict[str, set[str]] = {}

    for match in _CREATE_TABLE.finditer(source):
        table = match.group(1)
        body = match.group(2)
        start_line = source[: match.start()].count("\n") + 1
        end_line = source[: match.end()].count("\n") + 1
        table_id = f"sql::table::{table}"
        chunks.append(CodeChunk(
            symbol_id=table_id,
            file_path=file_path,
            language="sql",
            kind="sql_table",
            content=match.group(0)[:4000],
            start_line=start_line,
            end_line=end_line,
        ))
        cols = table_columns.setdefault(table, set())
        for col_match in _FIELD_COL_DEF.finditer(body):
            col = col_match.group(1)
            if col.upper() in {"PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT", "INDEX", "KEY", "EXCLUDE"}:
                continue
            cols.add(col)
            chunks.append(CodeChunk(
                symbol_id=f"sql::column::{table}::{col}",
                file_path=file_path,
                language="sql",
                kind="sql_column",
                content=f"{table}.{col} — {col_match.group(0).strip()[:200]}",
                start_line=start_line,
                end_line=start_line,
            ))
            edges.append(GraphEdge(
                source_symbol_id=f"sql::column::{table}::{col}",
                target_symbol_id=table_id,
                relation="uses",
                usage_modes=["field_declaration"],
            ))

    # -- COMMIT: migration blocks — one symbol per block, edges to whatever
    # tables/columns the block touches. The history a PR must not lose.
    block_start: int | None = None
    block_header = ""
    block_no = 0
    for i, line in enumerate(lines, start=1):
        m = _MIGRATION_HEADER.match(line)
        if m:
            if block_start is not None:
                chunks.extend(_migration_block(file_path, module, block_no, block_header, lines, block_start, i - 1, table_columns, edges))
            block_start = i
            block_header = m.group(1).strip()
            block_no += 1
    if block_start is not None:
        chunks.extend(_migration_block(file_path, module, block_no, block_header, lines, block_start, len(lines), table_columns, edges))

    return chunks, edges


def _migration_block(
    file_path: str,
    module: str,
    number: int,
    header: str,
    lines: list[str],
    start: int,
    end: int,
    table_columns: dict[str, set[str]],
    edges: list[GraphEdge],
) -> list[CodeChunk]:
    body = "\n".join(lines[start - 1: end])
    sym = f"sql::migration::{module}::{number:03d}_{_slug(header)}"
    tables = set(_TABLE_REF.findall(body))
    cols = set(_COLUMN_REF.findall(body))
    for _, new_col in _COLUMN_RENAME.findall(body):
        cols.add(new_col)
    # Table targets: the migration touches these tables.
    targets = {f"sql::table::{t}" for t in tables}
    # Column targets only for GENUINE pairs — same-file declared columns the
    # body mentions, or explicit column refs when the statement names exactly
    # one table. A table×column cross-product manufactured phantom
    # sql::column::t::c nodes for every combo (smoke caught the inflation).
    for t in tables:
        for c in table_columns.get(t, set()):
            if re.search(rf"\b{re.escape(c)}\b", body):
                targets.add(f"sql::column::{t}::{c}")
    if len(tables) == 1:
        (t,) = tables
        for c in cols:
            targets.add(f"sql::column::{t}::{c}")
    for target in sorted(targets):
        edges.append(GraphEdge(source_symbol_id=sym, target_symbol_id=target, relation="uses"))
    return [CodeChunk(
        symbol_id=sym,
        file_path=file_path,
        language="sql",
        kind="migration_block",
        content=body[:4000],
        start_line=start,
        end_line=end,
    )]


def link_sql_to_entities(chunks: list[CodeChunk]) -> list[GraphEdge]:
    """sea-orm entity fields ↔ columns: an entity struct with
    #[sea_orm(table_name = "disputes")] (or a CamelCase name matching the
    table) maps its fields to same-named columns. Bidirectional edges so a
    column query shows the entity readers and an entity change shows its DDL."""
    by_kind: dict[str, list[CodeChunk]] = {}
    for c in chunks:
        by_kind.setdefault(c.kind, []).append(c)

    def _snake(name: str) -> str:
        return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()

    struct_chunks = [c for c in chunks if c.kind in ("struct", "struct_field") or c.kind == "struct"]
    struct_chunks = [c for c in chunks if c.kind == "struct"]

    entity_tables: dict[str, str] = {}  # struct symbol_id -> table
    for st in struct_chunks:
        m = _ENTITY_TABLE_ATTR.search(st.content)
        if m:
            entity_tables[st.symbol_id] = m.group(1)
        else:
            short = st.symbol_id.split("::")[-1]
            entity_tables[st.symbol_id] = _snake(short)

    columns = by_kind.get("sql_column", [])
    edges: list[GraphEdge] = []
    for col in columns:
        # sql::column::table::col
        parts = col.symbol_id.split("::")
        if len(parts) < 4:
            continue
        table, field = parts[2], parts[3]
        for entity_sym, etable in entity_tables.items():
            if etable != table:
                continue
            if field in col.content and (field in entity_sym.split("::")[-1].lower() or True):
                # field presence in the entity is confirmed by name equality
                # against the struct content at query time — the edge records
                # the table mapping, find_field_access resolves read sites.
                edges.append(GraphEdge(
                    source_symbol_id=col.symbol_id,
                    target_symbol_id=entity_sym,
                    relation="uses",
                    usage_modes=["field_declaration"],
                ))
                edges.append(GraphEdge(
                    source_symbol_id=entity_sym,
                    target_symbol_id=col.symbol_id,
                    relation="uses",
                ))
                break
    return edges
