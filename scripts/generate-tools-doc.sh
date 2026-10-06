#!/usr/bin/env bash
# Regenerate the tool table section of docs/tools.md from the LIVE registry.
#
# Usage: ./scripts/generate-tools-doc.sh
# The drift test (test_tools_doc_contract) fails CI until this is re-run
# after any tool addition/rename/description change — run it in the same
# commit as the registry change.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DOC="$PROJECT_ROOT/docs/tools.md"

if [ ! -f "$DOC" ]; then
    echo "ERROR: $DOC does not exist yet" >&2
    exit 1
fi

# render from the running container's registry (always the deployed truth)
NEW_TABLE="$(docker exec oncall-backend python -c "from app.mcp.tools_doc import render_tools_table; print(render_tools_table())")"

python3 - "$DOC" "$NEW_TABLE" <<'EOF'
import sys

doc_path, new_table = sys.argv[1], sys.argv[2]
text = open(doc_path).read()
BEGIN = "<!-- BEGIN GENERATED TOOL TABLE -->"
END = "<!-- END GENERATED TOOL TABLE -->"
if BEGIN not in text or END not in text:
    sys.exit("markers missing from docs/tools.md")
start = text.index(BEGIN)
end = text.index(END) + len(END)
open(doc_path, "w").write(text[:start] + new_table.strip() + "\n\n" + text[end:])
print("docs/tools.md tool table regenerated")
EOF
