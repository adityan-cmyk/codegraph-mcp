#!/usr/bin/env bash
# Safe test runner — runs the unit test suite in a container with NO network.
#
# Why: on 2026-09-29 a `docker compose run backend pytest` leaked the compose
# env into the test process. The test setUp() reset() calls then wiped the
# LIVE Neo4j graph, Weaviate collection, and Postgres build registry — a
# total index outage recovered only by a full re-index from source.
#
# This script makes that structurally impossible: `--network none` means there
# is no network path to Postgres, Neo4j, Weaviate, or Redis, regardless of
# what env the caller has. It mounts only app/ and tests/ so the image's
# preloaded embedding model at /app/.cache remains available.
#
# Usage: ./scripts/run-tests.sh [pytest args...]
#   e.g. ./scripts/run-tests.sh tests/unit -q
#        ./scripts/run-tests.sh tests/unit/test_rag_and_mcp.py -k boost -v

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

IMAGE="$(docker compose -f "$PROJECT_ROOT/docker-compose.yml" images -q backend)"
if [ -z "$IMAGE" ]; then
    echo "ERROR: backend image not built yet — run: docker compose build backend" >&2
    exit 1
fi

docker run --rm --network none \
    -v "$PROJECT_ROOT/backend/app:/app/app:ro" \
    -v "$PROJECT_ROOT/backend/tests:/app/tests:ro" \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -e HF_HOME=/app/.cache/huggingface \
    --user "$(id -u):$(id -g)" \
    "$IMAGE" \
    python -m pytest -p no:cacheprovider "$@"
