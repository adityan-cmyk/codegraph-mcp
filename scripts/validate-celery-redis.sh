#!/usr/bin/env bash
set -euo pipefail

echo "=== Redis + Celery Worker Validation ==="
echo ""

API_BASE="${API_BASE:-http://localhost:8000}"

echo "1. Checking Redis health..."
REDIS_STATUS=$(curl -sS "$API_BASE/api/health" | jq -r '.backends[] | select(.backend=="redis") | .status')
echo "   Redis status: $REDIS_STATUS"

if [ "$REDIS_STATUS" != "healthy" ]; then
    echo "   FAIL  Redis is not healthy"
    exit 1
fi

echo "2. Creating a resolved incident for KB sync..."
SESSION_ID=$(curl -sS -X POST "$API_BASE/api/incidents/" \
    -H "Content-Type: application/json" \
    -d '{
        "fingerprint": {"service":"celery-test","panic_type":"test","top_frame":"src/celery.rs:1","commit_hash":"celery123"},
        "environment":"UAT",
        "build_id":"celery-build",
        "raw_log":"celery validation test",
        "source":"validation"
    }' | jq -r '.session_id')
echo "   Session: $SESSION_ID"

echo "3. Transitioning through states to RESOLVED..."
for state in INGESTING RETRIEVING GRAPH_EXPANDING GENERATING_PATCH VALIDATING RESOLVED; do
    curl -sS -X POST "$API_BASE/api/incidents/$SESSION_ID/state" \
        -H "Content-Type: application/json" \
        -d "{\"next_state\":\"$state\",\"event_type\":\"test_transition\"}" > /dev/null
done

CURRENT_STATE=$(curl -sS "$API_BASE/api/incidents/$SESSION_ID" | jq -r '.state')
echo "   Current state: $CURRENT_STATE"

echo "4. Triggering KB sync..."
SYNC_RESPONSE=$(curl -sS -X POST "$API_BASE/api/kb/sync" \
    -H "Content-Type: application/json" \
    -d '{"old_commit":"abc1234","new_commit":"def5678"}')

TASK_ID=$(echo "$SYNC_RESPONSE" | jq -r '.task_id')
TASK_STATUS=$(echo "$SYNC_RESPONSE" | jq -r '.status')
echo "   Task ID: $TASK_ID"
echo "   Task status: $TASK_STATUS"

if [ "$TASK_STATUS" = "queued" ] || [ "$TASK_STATUS" = "completed" ]; then
    echo "   PASS  KB sync task accepted"
else
    echo "   FAIL  KB sync task not accepted (status: $TASK_STATUS)"
    exit 1
fi

echo ""
echo "=== Redis + Celery Validation Complete ==="
