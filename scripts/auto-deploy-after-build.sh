#!/usr/bin/env bash
# One-shot: wait for the current build to finish, then deploy the pending
# backend image (X-Forwarded-For rate-limit fix, b8d70b8).
# Deploys on completion OR failure (nothing to protect once the build is over).
# Hard timeout 90 min so a silent stall still deploys.

set -u
LOG=/home/adi/repos/on-call-assistance/logs/auto-deploy.log
echo "[$(date)] watcher started" >> "$LOG"

for i in $(seq 1 90); do
    if docker logs oncall-backend 2>&1 | grep -qE "Incremental reindex complete|Build FAILED"; then
        echo "[$(date)] build finished — deploying" >> "$LOG"
        cd /home/adi/repos/on-call-assistance
        docker compose up -d backend >> "$LOG" 2>&1
        sleep 15
        if docker logs oncall-backend --since 1m 2>&1 | grep -qE "ERROR|Traceback"; then
            echo "[$(date)] WARNING: errors after deploy — check logs" >> "$LOG"
        else
            echo "[$(date)] deployed clean" >> "$LOG"
        fi
        exit 0
    fi
    sleep 60
done

echo "[$(date)] 90-min timeout — deploying anyway" >> "$LOG"
cd /home/adi/repos/on-call-assistance
docker compose up -d backend >> "$LOG" 2>&1
echo "[$(date)] deployed (timeout path)" >> "$LOG"
