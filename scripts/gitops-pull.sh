#!/usr/bin/env bash
# GitOps: converge the running backend to origin/main.
# Push to main = deployed. Never builds a dirty tree, never discards unpushed
# local commits, and a failed build never deploys (old image keeps running).
# One box, no registry needed — the image-diff deployer handles the final hop.
export PATH=/usr/bin:/bin:/usr/local/bin:$HOME/.local/bin
set -u
cd /home/adi/repos/on-call-assistance
LOG=logs/gitops.log
IMAGE=on-call-assistance-backend
MARK=logs/.gitops-blocked-marker

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

alert() {
    docker exec oncall-backend python -c "
from app.core.notifications import send_email
send_email(subject='''$1''', body_html='<div>$2</div>')
" >> "$LOG" 2>&1 || true
}

# Alert at most once per distinct blocked state.
blocked() {
    log "$1"
    if [ "$(cat "$MARK" 2>/dev/null)" != "$2" ]; then
        echo "$2" > "$MARK"
        alert "$3" "$4"
    fi
    exit 0
}

git fetch origin --quiet || { log "git fetch failed"; exit 0; }
REMOTE=$(git rev-parse origin/main)
SHORT=${REMOTE:0:8}

# Already converged? (running image tagged with the remote SHA)
RUNNING_ID=$(docker inspect oncall-backend --format '{{.Image}}' 2>/dev/null) || exit 0
DEPLOYED=$(docker inspect --type image "$RUNNING_ID" --format '{{join .RepoTags "\n"}}' 2>/dev/null \
    | grep -oE ':[0-9a-f]{8}$' | tr -d ':' | head -1)
if [ "$DEPLOYED" = "$SHORT" ]; then
    rm -f "$MARK"
    exit 0
fi

# Never build a dirty tree — the image must match a git SHA exactly.
if ! git diff --quiet || ! git diff --cached --quiet; then
    blocked "origin/main moved to $SHORT but working tree is dirty — refusing to build" \
        "dirty:$SHORT" \
        "[codegraph] GitOps blocked: dirty tree" \
        "origin/main is at $SHORT but the working tree has uncommitted changes. Commit or stash, then the next run deploys automatically."
fi

# Fast-forward only — never discard unpushed local commits.
if ! git merge --ff-only origin/main --quiet 2>/dev/null; then
    blocked "local main and origin/main diverged (unpushed commits?) — refusing to touch" \
        "diverged:$SHORT" \
        "[codegraph] GitOps blocked: diverged branches" \
        "local main has commits not on origin/main (or vice versa). Push or reconcile, then the next run deploys automatically."
fi

log "origin/main $SHORT != deployed ${DEPLOYED:-none} — building"
if ! docker compose build backend > /dev/null 2>&1; then
    blocked "BUILD FAILED for $SHORT — keeping ${DEPLOYED:-old image} running" \
        "buildfail:$SHORT" \
        "[codegraph ALERT] GitOps build failed: $SHORT" \
        "docker compose build failed for $SHORT. The previously deployed image keeps running. Reproduce: <code>docker compose build backend</code>"
fi
# Tag the built image with the SHA ourselves — if the content is identical to
# what's running, the deployer correctly no-ops, and this tag still marks the
# running image as converged (otherwise we'd rebuild forever on doc commits).
docker tag "$IMAGE:latest" "$IMAGE:$SHORT"
log "built $SHORT — auto-deployer will converge within a minute"
rm -f "$MARK"
