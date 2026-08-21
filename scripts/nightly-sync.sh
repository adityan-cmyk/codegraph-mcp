#!/bin/bash
# Nightly sync — pull latest code and trigger reindex
# Run via cron at 12am: 0 0 * * * /path/to/on-call-assistance/scripts/nightly-sync.sh
#
# ONLY git pull is allowed. No other git write commands (push, checkout, reset, merge, etc.)
#
# Required env vars (set in .env or shell):
#   CODEBASE_REPO_PATH — path to the codebase repo on the host
#   CODEBASE_GIT_BRANCH — branch to pull (default: master)

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

# Step 1: Git pull ONLY — verify we're only running 'git pull', nothing else
log "Pulling latest from origin/${GIT_BRANCH}..."
cd "$REPO_PATH"

# Explicitly only run git pull. No checkout, reset, merge, push, rebase, etc.
git pull --ff-only origin "$GIT_BRANCH" 2>&1

log "Git pull complete"

# Step 2: Trigger incremental reindex via API (git diff based, not full rebuild)
log "Triggering incremental reindex..."
RESPONSE=$(curl -s --max-time 14400 -X POST "$API_URL/api/index/ingest" 2>&1) || true
log "Reindex response: $RESPONSE"

log "Nightly sync complete"
