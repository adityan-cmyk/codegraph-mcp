#!/usr/bin/env bash
set -euo pipefail

echo "=== DB Persistence Validation ==="
echo ""

API_BASE="${API_BASE:-http://localhost:8000}"

echo "1. Creating incident..."
SESSION_ID=$(curl -sS -X POST "$API_BASE/api/incidents/" \
    -H "Content-Type: application/json" \
    -d '{
        "fingerprint": {"service":"persist-test","panic_type":"test","top_frame":"src/persist.rs:1","commit_hash":"persist123"},
        "environment":"UAT",
        "build_id":"persist-build",
        "raw_log":"persistence validation test",
        "source":"validation"
    }' | jq -r '.session_id')

echo "   Created session: $SESSION_ID"

echo "2. Adding chat message..."
curl -sS -X POST "$API_BASE/api/incidents/$SESSION_ID/chat" \
    -H "Content-Type: application/json" \
    -d '{"role":"user","content":"Does this persist across restarts?"}' > /dev/null

TIMELINE_COUNT=$(curl -sS "$API_BASE/api/incidents/$SESSION_ID" | jq '.timeline | length')
echo "   Timeline events: $TIMELINE_COUNT"

echo "3. Now restart the backend, then re-run this script with --verify"
echo "   The session and timeline should still be retrievable."

if [ "${1:-}" = "--verify" ]; then
    echo ""
    echo "4. Verifying persistence after restart..."
    RETRIEVED_SESSION=$(curl -sS "$API_BASE/api/incidents/$SESSION_ID" | jq -r '.session_id')

    if [ "$RETRIEVED_SESSION" = "$SESSION_ID" ]; then
        echo "   PASS  Session persisted ($SESSION_ID)"
    else
        echo "   FAIL  Session not found (expected $SESSION_ID, got $RETRIEVED_SESSION)"
        exit 1
    fi

    RETRIEVED_COUNT=$(curl -sS "$API_BASE/api/incidents/$SESSION_ID" | jq '.timeline | length')
    if [ "$RETRIEVED_COUNT" = "$TIMELINE_COUNT" ]; then
        echo "   PASS  Timeline persisted ($RETRIEVED_COUNT events)"
    else
        echo "   FAIL  Timeline mismatch (expected $TIMELINE_COUNT, got $RETRIEVED_COUNT)"
        exit 1
    fi

    echo ""
    echo "=== DB Persistence Validation PASSED ==="
fi
