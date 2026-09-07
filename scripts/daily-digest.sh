#!/bin/bash
# Daily digest — sends the usage/health summary email at 6pm via the backend API.
# Run via cron: 0 18 * * * /path/to/scripts/daily-digest.sh >> /path/to/logs/daily-digest.log 2>&1

set -euo pipefail

API_URL="http://localhost:8000"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "Sending daily digest..."
RESPONSE=$(curl -s --max-time 120 -X POST "$API_URL/api/index/digest" 2>&1) || true
log "Digest response: $RESPONSE"
