#!/bin/bash
# Nightly sync — pull latest code and trigger reindex
# Run via cron at 12am: 0 0 * * * /path/to/on-call-assistance/scripts/nightly-sync.sh
#
# ONLY git pull is allowed. No other git write commands (push, checkout, reset, merge, etc.)
#
# Required env vars (set in .env or shell):
#   CODEBASE_REPO_PATH — path to the codebase repo on the host
#   CODEBASE_GIT_BRANCH — branch to pull (default: master)
#   CODEBASE_GIT_REMOTE — git remote host for DNS check (optional, for network resilience)

set -euo pipefail

# Source .env if available (cron doesn't inherit shell env)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
if [[ -f "$PROJECT_ROOT/.env" ]]; then
    set -a
    source "$PROJECT_ROOT/.env"
    set +a
fi

REPO_PATH="${CODEBASE_REPO_PATH:?CODEBASE_REPO_PATH must be set}"
GIT_BRANCH="${CODEBASE_GIT_BRANCH:-master}"
API_URL="http://localhost:8000"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "Starting nightly sync"

# Wait for DNS to be ready (cron may run before network is fully up)
if [[ -n "${CODEBASE_GIT_REMOTE:-}" ]]; then
    for i in $(seq 1 10); do
        if getent hosts "$CODEBASE_GIT_REMOTE" >/dev/null 2>&1; then
            break
        fi
        log "DNS not ready, retrying ($i/10)..."
        sleep 30
    done
fi

# Step 1: Git pull ONLY — verify we're only running 'git pull', nothing else
log "Pulling latest from origin/${GIT_BRANCH}..."
cd "$REPO_PATH"

# Explicitly only run git pull. No checkout, reset, merge, push, rebase, etc.
# Retry up to 3 times in case of transient network issues
GIT_PULL_ERROR=""
for attempt in 1 2 3; do
    if GIT_PULL_ERROR=$(git pull --ff-only origin "$GIT_BRANCH" 2>&1); then
        echo "$GIT_PULL_ERROR"
        break
    fi
    if [[ $attempt -lt 3 ]]; then
        log "Git pull failed (attempt $attempt/3), retrying in 60s..."
        sleep 60
    else
        log "Git pull failed after 3 attempts: $GIT_PULL_ERROR"
        curl -s -X POST "$API_URL/api/index/notify/nightly-sync-failed" \
            -H "Content-Type: application/json" \
            -H "Authorization: Bearer ${API_AUTH_TOKEN:-}" \
            -d "{\"stage\": \"git_pull\", \"error\": \"$(echo "$GIT_PULL_ERROR" | head -c 500 | sed 's/"/\\"/g' | tr '\n' ' ')\"}" || true
        exit 1
    fi
done

log "Git pull complete"

# Step 2: Trigger incremental reindex via API (git diff based, not full rebuild)
# A rebuild already running is a WAIT, not a failure — _REBUILD_MUTEX
# serializes builds and answers 409; racing it must not fail the night.
log "Triggering incremental reindex..."
MAX_WAITS=10
for attempt in $(seq 1 "$MAX_WAITS"); do
    RESPONSE=$(curl -s --max-time 14400 -X POST -H "Authorization: Bearer ${API_AUTH_TOKEN:-}" "$API_URL/api/index/ingest" 2>&1) || true
    log "Reindex response (attempt $attempt/$MAX_WAITS): $RESPONSE"

    if ! echo "$RESPONSE" | grep -qE '"detail"|"error"'; then
        break
    fi
    if echo "$RESPONSE" | grep -qiE 'rebuild is already in progress|RebuildInProgress'; then
        log "Another rebuild is running — waiting 3 min before retry"
        sleep 180
        continue
    fi

    # an error response is a FAILED sync, not a completed one — the 2026-10-07
    # placeholder-mismatch bug logged "Nightly sync complete" over an error body
    # and nobody was alerted
    log "INCREMENTAL REINDEX FAILED — error in response"
    SAFE_RESP=$(echo "$RESPONSE" | head -c 400 | tr -d '"')
    docker exec oncall-backend python -c "
from app.core.notifications import send_email
send_email(subject='[codegraph ALERT] Nightly incremental reindex FAILED',
           body_html='<div>The incremental ingest returned an error — the index stays behind HEAD until fixed. Response: <pre>${SAFE_RESP}</pre></div>')
" >> "$LOG" 2>&1 || true
    exit 1
done

# Exhausted the waits and the rebuild is still running: deferred, not failed.
# Ingest is commit-pointer based — the next run catches up automatically.
if echo "$RESPONSE" | grep -qiE 'rebuild is already in progress|RebuildInProgress'; then
    log "Deferred: a rebuild ran all night — skipping (not a failure, next run catches up)"
    exit 0
fi

log "Nightly sync complete"
