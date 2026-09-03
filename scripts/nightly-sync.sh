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
            -d "{\"stage\": \"git_pull\", \"error\": \"$(echo "$GIT_PULL_ERROR" | head -c 500 | sed 's/"/\\"/g' | tr '\n' ' ')\"}" || true
        exit 1
    fi
done

log "Git pull complete"

# Step 2: Trigger incremental reindex via API (git diff based, not full rebuild)
log "Triggering incremental reindex..."
RESPONSE=$(curl -s --max-time 14400 -X POST "$API_URL/api/index/ingest" 2>&1) || true
log "Reindex response: $RESPONSE"

log "Nightly sync complete"
