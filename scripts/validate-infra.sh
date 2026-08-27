#!/usr/bin/env bash
set -euo pipefail

echo "=== Codegraph-MCP Infrastructure Validation ==="
echo ""

API_BASE="${API_BASE:-http://localhost:8000}"
MCP_URL="${MCP_URL:-http://localhost:8002/mcp}"
PASS=0
FAIL=0
SKIP=0

check() {
    local label="$1"
    local url="$2"
    local expected_status="${3:-200}"
    status_code=$(curl -sS -o /dev/null -w "%{http_code}" "$url" 2>/dev/null) || status_code="000"
    if [ "$status_code" = "$expected_status" ]; then
        echo "  PASS  $label ($status_code)"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  $label (expected $expected_status, got $status_code)"
        FAIL=$((FAIL + 1))
    fi
}

check_json() {
    local label="$1"
    local url="$2"
    local jq_expr="$3"
    local expected="$4"
    value=$(curl -sS "$url" 2>/dev/null | jq -r "$jq_expr" 2>/dev/null) || value="ERROR"
    if [ "$value" = "$expected" ]; then
        echo "  PASS  $label ($value)"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  $label (expected $expected, got $value)"
        FAIL=$((FAIL + 1))
    fi
}

mcp_call() {
    local method="$1"
    local params="$2"
    curl -sS -X POST "$MCP_URL" \
        -H "Content-Type: application/json" \
        -d "{\"jsonrpc\":\"2.0\",\"method\":\"$method\",\"params\":$params,\"id\":1}" 2>/dev/null
}

echo "--- Container Health ---"
for svc in oncall-postgres oncall-redis oncall-weaviate oncall-neo4j oncall-backend; do
    status=$(docker inspect --format='{{.State.Status}}' "$svc" 2>/dev/null) || status="missing"
    if [ "$status" = "running" ]; then
        echo "  PASS  $svc (running)"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  $svc ($status)"
        FAIL=$((FAIL + 1))
    fi
done

echo ""
echo "--- API Health ---"
check "Health endpoint" "$API_BASE/health"
check "Detailed health" "$API_BASE/api/health"
check "Metrics endpoint" "$API_BASE/api/metrics"

echo ""
echo "--- Backend Connectivity ---"
check_json "Postgres" "$API_BASE/api/health" '.backends[] | select(.backend=="postgres") | .status' "healthy"
check_json "Redis" "$API_BASE/api/health" '.backends[] | select(.backend=="redis") | .status' "healthy"
check_json "Weaviate" "$API_BASE/api/health" '.backends[] | select(.backend=="weaviate") | .status' "healthy"
check_json "Neo4j" "$API_BASE/api/health" '.backends[] | select(.backend=="neo4j") | .status' "healthy"

echo ""
echo "--- Index Stats ---"
STATS=$(curl -sS "$API_BASE/api/index/stats" 2>/dev/null) || STATS="{}"
GRAPH_NODES=$(echo "$STATS" | jq -r '.graph_nodes // 0')
GRAPH_EDGES=$(echo "$STATS" | jq -r '.graph_edges // 0')
SEMANTIC_DOCS=$(echo "$STATS" | jq -r '.semantic_documents // 0')
REBUILD=$(echo "$STATS" | jq -r '.semantic_rebuild_in_progress // false')
echo "  Graph: $GRAPH_NODES nodes, $GRAPH_EDGES edges"
echo "  Semantic: $SEMANTIC_DOCS documents"
echo "  Rebuild in progress: $REBUILD"
if [ "$GRAPH_NODES" -gt 0 ] && [ "$SEMANTIC_DOCS" -gt 0 ]; then
    echo "  PASS  Index populated"
    PASS=$((PASS + 1))
else
    echo "  FAIL  Index empty (graph=$GRAPH_NODES, semantic=$SEMANTIC_DOCS)"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "--- Index Freshness ---"
INGEST_STATUS=$(curl -sS "$API_BASE/api/index/ingest/status" 2>/dev/null) || INGEST_STATUS="{}"
UP_TO_DATE=$(echo "$INGEST_STATUS" | jq -r '.up_to_date // false')
LAST_COMMIT=$(echo "$INGEST_STATUS" | jq -r '.last_indexed_commit // "none"' | cut -c1-8)
HEAD_COMMIT=$(echo "$INGEST_STATUS" | jq -r '.current_head // "none"' | cut -c1-8)
echo "  Last indexed: $LAST_COMMIT"
echo "  Current head: $HEAD_COMMIT"
if [ "$UP_TO_DATE" = "true" ]; then
    echo "  PASS  Index up-to-date"
    PASS=$((PASS + 1))
else
    echo "  WARN  Index behind head (run nightly-sync or POST /api/index/ingest)"
    SKIP=$((SKIP + 1))
fi

echo ""
echo "--- MCP Server ---"
MCP_TOOLS=$(mcp_call "tools/list" '{}' | jq -r '.result.tools | length' 2>/dev/null) || MCP_TOOLS=0
if [ "$MCP_TOOLS" -gt 0 ]; then
    echo "  PASS  MCP tools/list ($MCP_TOOLS tools)"
    PASS=$((PASS + 1))
else
    echo "  FAIL  MCP tools/list (no tools returned)"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "--- MCP: Graph Stats ---"
GRAPH_STATS=$(mcp_call "tools/call" '{"name":"get_graph_stats","arguments":{}}' | jq -r '.result.content[0].text' 2>/dev/null) || GRAPH_STATS="{}"
MCP_NODES=$(echo "$GRAPH_STATS" | jq -r '.graph_nodes // 0')
MCP_EDGES=$(echo "$GRAPH_STATS" | jq -r '.graph_edges // 0')
if [ "$MCP_NODES" -gt 0 ]; then
    echo "  PASS  get_graph_stats ($MCP_NODES nodes, $MCP_EDGES edges)"
    PASS=$((PASS + 1))
else
    echo "  FAIL  get_graph_stats (empty)"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "--- MCP: Index Meta ---"
META=$(mcp_call "tools/call" '{"name":"get_index_meta","arguments":{}}' | jq -r '.result.content[0].text' 2>/dev/null) || META="{}"
GEN=$(echo "$META" | jq -r '.graph_gen // "?"')
MODEL=$(echo "$META" | jq -r '.embedding_model // "?"')
COLLECTION=$(echo "$META" | jq -r '.weaviate_collection // "?"')
echo "  Gen: $GEN | Model: $MODEL | Collection: $COLLECTION"
if [ "$GEN" != "?" ]; then
    echo "  PASS  get_index_meta"
    PASS=$((PASS + 1))
else
    echo "  FAIL  get_index_meta"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "--- MCP: Semantic Search ---"
SEM_RESULT=$(mcp_call "tools/call" '{"name":"semantic_search","arguments":{"query":"payment transaction processing","limit":3}}' 2>/dev/null)
SEM_MATCHES=$(echo "$SEM_RESULT" | jq -r '.result.content[0].text' 2>/dev/null | jq -r '.matches // 0' 2>/dev/null) || SEM_MATCHES=0
if [ "$SEM_MATCHES" -gt 0 ]; then
    echo "  PASS  semantic_search ($SEM_MATCHES matches)"
    PASS=$((PASS + 1))
else
    echo "  FAIL  semantic_search (no matches)"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "--- MCP: Symbol Search ---"
SYM_RESULT=$(mcp_call "tools/call" '{"name":"search_symbols","arguments":{"query":"wallet"}}' 2>/dev/null)
SYM_COUNT=$(echo "$SYM_RESULT" | jq -r '.result.content[0].text' 2>/dev/null | jq -r '.symbols | length' 2>/dev/null) || SYM_COUNT=0
if [ "$SYM_COUNT" -gt 0 ]; then
    echo "  PASS  search_symbols ($SYM_COUNT results)"
    PASS=$((PASS + 1))
else
    echo "  FAIL  search_symbols (no results)"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "--- Incident Lifecycle ---"
SESSION_ID=$(curl -sS -X POST "$API_BASE/api/incidents/" \
    -H "Content-Type: application/json" \
    -d '{
        "fingerprint": {"service":"validation","panic_type":"test","top_frame":"src/val.rs:1","commit_hash":"val123"},
        "environment":"UAT",
        "build_id":"val-build",
        "raw_log":"validation test panic",
        "source":"validation"
    }' | jq -r '.session_id') || SESSION_ID=""

if [ -n "$SESSION_ID" ] && [ "$SESSION_ID" != "null" ]; then
    echo "  PASS  Create incident ($SESSION_ID)"
    PASS=$((PASS + 1))
    check "Get incident" "$API_BASE/api/incidents/$SESSION_ID"
    check "List incidents" "$API_BASE/api/incidents/"
else
    echo "  FAIL  Create incident"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "=== Results: $PASS passed, $FAIL failed, $SKIP warnings ==="

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
