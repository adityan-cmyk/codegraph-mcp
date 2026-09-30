#!/bin/bash
# Daily digest — sends the usage/health summary email at 6pm via the backend API.
# Run via cron: 0 18 * * * /path/to/scripts/daily-digest.sh >> /path/to/logs/daily-digest.log 2>&1

set -euo pipefail

API_URL="http://localhost:8000"

# Source .env for API_AUTH_TOKEN (cron doesn't inherit shell env)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
if [[ -f "$PROJECT_ROOT/.env" ]]; then
    source "$PROJECT_ROOT/.env"
fi
AUTH_HEADER=()
[[ -n "${API_AUTH_TOKEN:-}" ]] && AUTH_HEADER=(-H "Authorization: Bearer $API_AUTH_TOKEN")

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "Sending daily digest..."
RESPONSE=$(curl -s --max-time 120 -X POST "${AUTH_HEADER[@]}" "$API_URL/api/index/digest" 2>&1) || true
log "Digest response: $RESPONSE"
