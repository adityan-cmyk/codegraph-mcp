#!/usr/bin/env bash
set -euo pipefail

echo "=== Oncall Infrastructure Validation ==="
echo ""

API_BASE="${API_BASE:-http://localhost:8000}"
PASS=0
FAIL=0

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

echo "--- Basic Health ---"
check "Health endpoint" "$API_BASE/health"
check "Detailed health" "$API_BASE/api/health"
check "Metrics endpoint" "$API_BASE/api/metrics"
check "Model status" "$API_BASE/api/model/status"
check "Tracing status" "$API_BASE/api/tracing/status"

echo ""
echo "--- Backend Connectivity ---"
check_json "Postgres status" "$API_BASE/api/health" '.backends[] | select(.backend=="postgres") | .status' "healthy"
check_json "Redis status" "$API_BASE/api/health" '.backends[] | select(.backend=="redis") | .status' "healthy"
check_json "Weaviate status" "$API_BASE/api/health" '.backends[] | select(.backend=="weaviate") | .status' "healthy"
check_json "Neo4j status" "$API_BASE/api/health" '.backends[] | select(.backend=="neo4j") | .status' "healthy"

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
echo "--- Index Endpoints ---"
check "Index stats" "$API_BASE/api/index/stats"
check "MCP status" "$API_BASE/api/mcp/status"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
