"""Render the canonical MCP tool table for docs/tools.md.

Shared by scripts/generate-tools-doc.py (which writes the committed doc) and
the contract-drift unit test (which fails when the committed table and the
live registry disagree). Same principle as the pinned registry test: docs
that cannot silently drift.

The generated section of docs/tools.md is bounded by markers; hand-written
usage guide content below the markers is not touched by the generator or
checked by the test.
"""

BEGIN_MARKER = "<!-- BEGIN GENERATED TOOL TABLE -->"
END_MARKER = "<!-- END GENERATED TOOL TABLE -->"

# tools that write (and only to feedback tables) — everything else is read-only
MUTATING_TOOLS = {"submit_search_feedback", "submit_ai_feedback"}


def render_tools_table() -> str:
    from app.mcp.readonly_server import _TOOLS, TOOL_SCHEMA_VERSION

    lines = [
        f"{BEGIN_MARKER}",
        f"Tool schema version: `{TOOL_SCHEMA_VERSION}` — {len(_TOOLS)} tools "
        f"({len(_TOOLS) - len(MUTATING_TOOLS)} read-only, "
        f"{len(MUTATING_TOOLS)} feedback-writing).",
        "",
        "| Tool | Mode | Description |",
        "|---|---|---|",
    ]
    for name in sorted(_TOOLS):
        spec = _TOOLS[name]
        mode = "mutating (feedback only)" if name in MUTATING_TOOLS else "read-only"
        desc = " ".join(str(spec.get("description", "")).split())
        lines.append(f"| `{name}` | {mode} | {desc} |")
    lines.append(END_MARKER)
    return "\n".join(lines)


def extract_generated_section(doc_text: str) -> str | None:
    """The marker-bounded generated block from docs/tools.md, or None."""
    if BEGIN_MARKER not in doc_text or END_MARKER not in doc_text:
        return None
    start = doc_text.index(BEGIN_MARKER)
    end = doc_text.index(END_MARKER) + len(END_MARKER)
    return doc_text[start:end]
