#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker-compose.db.yml"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is not installed or not in PATH"
  exit 1
fi

echo "Starting DB and infra services..."
docker compose -f "$COMPOSE_FILE" up -d

echo "Infra is up."
docker compose -f "$COMPOSE_FILE" ps
