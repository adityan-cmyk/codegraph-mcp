#!/usr/bin/env bash
# Generate a SCIP index of the codebase with rust-analyzer.
#
# SCIP (Source Code Intelligence Protocol) gives precise, type-resolved
# symbol definitions + references — vs the regex extractor's approximate
# edges. Runs in a one-off container (rust image), clones the repo to a
# writable location (the live mount is read-only), indexes, and writes the
# protobuf dump for the Python ingestor.
#
# Usage: bash scripts/generate-scip.sh [repo-rev]     (default: current HEAD)
# Output: scip-data/index.scip
#
# The scip-cache volume keeps the rust-analyzer binary and the cargo
# registry between runs — re-runs skip both downloads.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
OUT_DIR="$PROJECT_ROOT/scip-data"
mkdir -p "$OUT_DIR"

if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a; source "$PROJECT_ROOT/.env"; set +a
fi

REPO_HOST_PATH="${CODEBASE_REPO_PATH:?set CODEBASE_REPO_PATH in .env}"
REV="${1:-}"

docker run --rm \
  -v "$REPO_HOST_PATH:/source:ro" \
  -v "$OUT_DIR:/out" \
  -v scip_cache:/cache \
  -e CARGO_HOME=/cache/cargo \
  -w /work \
  rust:1-bookworm \
  bash -euo pipefail -c '
    set -x
    REV="'"${REV}"'"

    # rust-analyzer binary — cached between runs
    if [ ! -x /cache/rust-analyzer ]; then
      for attempt in 1 2 3 4 5; do
        if curl -fsSL -o /tmp/ra.gz \
          https://github.com/rust-lang/rust-analyzer/releases/latest/download/rust-analyzer-x86_64-unknown-linux-gnu.gz; then
          break
        fi
        echo "rust-analyzer download failed (attempt $attempt/5), retrying in 20s" >&2
        sleep 20
      done
      gunzip -c /tmp/ra.gz > /cache/rust-analyzer && chmod +x /cache/rust-analyzer
    fi
    ln -s /cache/rust-analyzer /usr/local/bin/rust-analyzer

    # local clone (no network needed for the clone itself)
    git config --global --add safe.directory "*"
    git clone --quiet /source /work/repo
    cd /work/repo
    if [ -n "$REV" ]; then git checkout --quiet "$REV"; fi
    git rev-parse HEAD > /out/source-rev.txt

    # dependency resolution — cached registry makes re-runs fast
    for attempt in 1 2 3 4 5; do
      if cargo fetch --quiet; then break; fi
      echo "cargo fetch failed (attempt $attempt/5), retrying in 30s" >&2
      sleep 30
    done

    # NOTE: rust-analyzer scip writes to a FILE (--output), not stdout
    rust-analyzer scip . --output /out/index.scip 2> /out/scip-stderr.log
    test -s /out/index.scip
    ls -la /out/index.scip
    echo "SCIP index generated at revision $(cat /out/source-rev.txt)"
  '

echo "done — $OUT_DIR/index.scip"
