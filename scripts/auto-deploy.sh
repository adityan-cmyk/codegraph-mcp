#!/usr/bin/env bash
# Stateless auto-deploy — cron every minute.
# Deploys when the built image (oncall-assistance-backend:latest) differs
# from the running container's image. No processes, no state files: the
# image diff IS the state. Survives reboots and session timeouts.
#
# Rollback (any time): docker tag oncall-assistance-backend:last-good \
#                          oncall-assistance-backend:latest && \
#                      docker compose up -d backend

export PATH=/usr/bin:/bin:/usr/local/bin:$HOME/.local/bin
set -u
cd /home/adi/repos/on-call-assistance
LOG=logs/auto-deploy.log
IMAGE=on-call-assistance-backend

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# Only act on a running container — the watchdog owns healing other states.
STATE=$(docker inspect oncall-backend --format '{{.State.Status}}' 2>/dev/null) || exit 0
[ "$STATE" = "running" ] || exit 0

LATEST_ID=$(docker inspect --type image "$IMAGE:latest" --format '{{.Id}}' 2>/dev/null) || exit 0
RUNNING_ID=$(docker inspect oncall-backend --format '{{.Image}}')
[ "$LATEST_ID" != "$RUNNING_ID" ] || exit 0

# Never deploy mid-indexing — a container restart loses build progress.
if docker logs oncall-backend --since 10m 2>&1 | grep -qE "Indexed batch|Incremental reindex|semantic rebuild|Build FAILED"; then
    log "new image waiting, indexing in progress — deferring"
    exit 0
fi

SHA=$(git rev-parse --short=8 HEAD)
log "deploying $SHA"
docker tag "$RUNNING_ID" "$IMAGE:last-good"
docker tag "$IMAGE:latest" "$IMAGE:$SHA"
docker compose up -d backend >> "$LOG" 2>&1

# Wait up to 180s for health (start_period is 120s).
OK=0
for _ in $(seq 1 36); do
    sleep 5
    [ "$(docker inspect oncall-backend --format '{{.State.Health.Status}}' 2>/dev/null)" = "healthy" ] && { OK=1; break; }
done

notify() {
    docker exec oncall-backend python -c "
from app.core.notifications import send_email
send_email(subject='''$1''', body_html='<div>$2</div>')
" >> "$LOG" 2>&1 || host_notify "$1" "$2"
}

host_notify() {
    set -a; . ./.env; set +a
    python3 - "$1" "$2" <<'PYEOF'
import json, os, smtplib, sys
from email.mime.text import MIMEText
subject, body = sys.argv[1], sys.argv[2]
m = MIMEText(body, "html")
m["Subject"] = subject
m["From"] = os.environ["SMTP_FROM"]
m["To"] = ", ".join(json.loads(os.environ["SMTP_TO"]))
with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])) as s:
    s.starttls()
    s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
    s.send_message(m)
PYEOF
}

if [ "$OK" = 1 ]; then
    docker tag "$LATEST_ID" "$IMAGE:last-good"
    log "deployed $SHA clean"
    notify "[codegraph] Auto-deploy: $SHA" "Deployed <code>$SHA</code> — container healthy. Tagged <code>$IMAGE:$SHA</code> and updated <code>last-good</code>."
else
    log "DEPLOY $SHA FAILED — unhealthy after 180s"
    notify "[codegraph ALERT] Auto-deploy failed: $SHA" "Container UNHEALTHY after deploying <code>$SHA</code>. Rollback: <code>docker tag $IMAGE:last-good $IMAGE:latest &amp;&amp; docker compose up -d backend</code>"
fi
