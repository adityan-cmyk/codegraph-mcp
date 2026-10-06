#!/usr/bin/env bash
# Nightly Postgres backup: pg_dump (gzip) + 7-day retention + RESTORE TEST.
# Postgres is the source of truth (snapshots, feedback, build registry,
# observations) — an unbacked-up source of truth is a promise you can't keep.
#
# The restore test runs after every dump: restores into a scratch database,
# counts rows in the key tables, drops it. An untested backup is a hope,
# not a backup.

export PATH=/usr/bin:/bin:/usr/local/bin:$HOME/.local/bin
set -euo pipefail
cd /home/adi/repos/on-call-assistance
LOG=logs/backup.log
BACKUP_DIR=backups/postgres
CONTAINER=oncall-postgres
DB=oncall
USER=oncall
KEEP_DAYS=7

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

alert() {
    docker exec oncall-backend python -c "
from app.core.notifications import send_email
send_email(subject='''$1''', body_html='<div>$2</div>')
" >> "$LOG" 2>&1 || true
}

mkdir -p "$BACKUP_DIR"
STAMP=$(date +%Y%m%d-%H%M%S)
DUMP="$BACKUP_DIR/oncall-$STAMP.sql.gz"

log "dumping to $DUMP"
if ! docker exec "$CONTAINER" pg_dump -U "$USER" "$DB" | gzip > "$DUMP"; then
    log "BACKUP FAILED — pg_dump error"
    alert "[codegraph ALERT] Postgres backup FAILED" "pg_dump failed at $STAMP. Check logs/backup.log and $DUMP."
    exit 1
fi

SIZE=$(du -h "$DUMP" | cut -f1)
log "dump complete ($SIZE) — running restore test"

# Restore test: load into a scratch DB and count the tables that matter.
VERIFY=$(docker exec -i "$CONTAINER" psql -U "$USER" -d postgres -v ON_ERROR_STOP=0 <<SQL
DROP DATABASE IF EXISTS oncall_restore_test;
CREATE DATABASE oncall_restore_test;
SQL
)
gunzip -c "$DUMP" | docker exec -i "$CONTAINER" psql -U "$USER" -d oncall_restore_test -q > /dev/null 2>&1 || true
COUNTS=$(docker exec -i "$CONTAINER" psql -U "$USER" -d oncall_restore_test -t -A <<SQL
SELECT 'search_feedback:' || COUNT(*) FROM search_feedback
UNION ALL SELECT 'ai_feedback:' || COUNT(*) FROM ai_feedback
UNION ALL SELECT 'symbol_reinforcement:' || COUNT(*) FROM symbol_reinforcement
UNION ALL SELECT 'build_registry:' || COUNT(*) FROM build_registry
UNION ALL SELECT 'code_observations:' || COUNT(*) FROM code_observations
UNION ALL SELECT 'index_snapshot:' || COUNT(*) FROM index_snapshots;
SQL
)
docker exec "$CONTAINER" psql -U "$USER" -d postgres -q -c "DROP DATABASE IF EXISTS oncall_restore_test;"

if echo "$COUNTS" | grep -q "index_snapshot:0"; then
    log "RESTORE TEST FAILED — snapshot table empty after restore"
    alert "[codegraph ALERT] Postgres restore test FAILED" "Backup $STAMP ($SIZE) restored but index_snapshots is empty — the dump may be incomplete. Check logs/backup.log."
    exit 1
fi

log "restore test passed: $(echo "$COUNTS" | tr '\n' ' ')"

# Retention: keep KEEP_DAYS of dailies.
DELETED=$(find "$BACKUP_DIR" -name "oncall-*.sql.gz" -mtime +$KEEP_DAYS -print -delete | wc -l)
log "retention: removed $DELETED old dumps"

# Alert if the newest backup is ever >25h old (cron died silently?)
LATEST=$(find "$BACKUP_DIR" -name "oncall-*.sql.gz" -mmin -1500 | wc -l)
if [ "$LATEST" -eq 0 ]; then
    alert "[codegraph ALERT] Postgres backup is stale" "No backup newer than 25h found in $BACKUP_DIR. Check the backup cron."
fi

# Off-site copy — OPTIONAL, disabled by default.
# Backups on the same machine as Postgres die with the machine. To make them
# protective, copy the nightly dump elsewhere (another tailnet box, a NAS,
# anything you own). Enable in .env:
#   OFFSITE_BACKUP_ENABLED=true
#   OFFSITE_BACKUP_TARGET=host:/remote/path   (any rsync-compatible target;
#                                              SSH key must already work)
# Read via grep (not `source .env`) so unrelated .env content can never
# break the backup path.
OFFSITE_BACKUP_ENABLED=$(grep -E '^OFFSITE_BACKUP_ENABLED=' .env 2>/dev/null | cut -d= -f2 || true)
OFFSITE_BACKUP_TARGET=$(grep -E '^OFFSITE_BACKUP_TARGET=' .env 2>/dev/null | cut -d= -f2 || true)
if [ "${OFFSITE_BACKUP_ENABLED:-false}" = "true" ] && [ -n "${OFFSITE_BACKUP_TARGET:-}" ]; then
    log "off-site copy to $OFFSITE_BACKUP_TARGET"
    COPIED=""
    for attempt in 1 2 3; do
        if rsync -az --timeout=120 "$DUMP" "$OFFSITE_BACKUP_TARGET/"; then
            COPIED="ok"
            break
        fi
        log "off-site copy failed (attempt $attempt/3)"
        sleep 20
    done
    if [ -z "$COPIED" ]; then
        log "OFF-SITE COPY FAILED after 3 attempts"
        alert "[codegraph ALERT] Off-site backup copy FAILED" "Backup $STAMP ($SIZE) could not be copied to $OFFSITE_BACKUP_TARGET after 3 attempts. Local dump is intact; check network/SSH to the target."
    else
        # retention on the remote mirrors local (best effort, non-fatal)
        ssh "${OFFSITE_BACKUP_TARGET%%:*}" \
            "find '${OFFSITE_BACKUP_TARGET#*:}' -name 'oncall-*.sql.gz' -mtime +$KEEP_DAYS -delete" \
            >> "$LOG" 2>&1 || log "remote retention skipped (non-fatal)"
        log "off-site copy complete"
    fi
fi

log "backup cycle complete"
