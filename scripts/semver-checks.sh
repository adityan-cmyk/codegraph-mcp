#!/usr/bin/env bash
# cargo-semver-checks — detect breaking public-API changes between two
# revisions of the codebase. Runs in a one-off container against a local
# clone (the live mount is read-only).
#
# Usage: bash scripts/semver-checks.sh [baseline-rev] [head-rev]
#   baseline: defaults to HEAD~30 (tunable)
#   head:     defaults to HEAD
#
# Only rust semver — workspace crates with public APIs are checked.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a; source "$PROJECT_ROOT/.env"; set +a
fi

REPO_HOST_PATH="${CODEBASE_REPO_PATH:?set CODEBASE_REPO_PATH in .env}"
BASELINE="${1:-HEAD~30}"
HEAD_REV="${2:-HEAD}"

echo "semver-checks: baseline=$BASELINE head=$HEAD_REV"

docker run --rm \
  -v "$REPO_HOST_PATH:/source:ro" \
  -v "$PROJECT_ROOT/logs:/out" \
  -w /work \
  rust:1-bookworm \
  bash -euo pipefail -c '
    set -x
    git config --global --add safe.directory "*"
    git clone --quiet /source /work/repo
    cd /work/repo

    # their deps can require a newer stable than the image ships
    # (aws-types needed 1.94.1 when the image had 1.94.0)
    for attempt in 1 2 3; do
      if rustup update stable 2>&1 | tail -1; then break; fi
      echo "rustup update failed (attempt $attempt/3)" >&2; sleep 20
    done
    rustup default stable
    # the repo pins 1.94.0 via rust-toolchain.toml but its current deps
    # need newer (aws-types requires 1.94.1+) — env override wins
    export RUSTUP_TOOLCHAIN=stable

    # cargo-semver-checks via cargo-binstall is faster than a source install
    for attempt in 1 2 3 4 5; do
      if curl -fsSL -o /tmp/binstall.tgz \
        https://github.com/cargo-bins/cargo-binstall/releases/latest/download/cargo-binstall-x86_64-unknown-linux-musl.tgz; then
        break
      fi
      echo "binstall download failed (attempt $attempt/5)" >&2; sleep 20
    done
    tar -xzf /tmp/binstall.tgz -C /usr/local/bin --strip-components=0 cargo-binstall/cargo-binstall 2>/dev/null \
      || tar -xzf /tmp/binstall.tgz -C /usr/local/bin
    find /usr/local/bin -name 'cargo-binstall*' -exec mv {} /usr/local/bin/cargo-binstall \; 2>/dev/null || true
    chmod +x /usr/local/bin/cargo-binstall
    cargo binstall --no-confirm cargo-semver-checks

    BASELINE="'"${BASELINE}"'"
    HEAD_REV="'"${HEAD_REV}"'"
    cargo semver-checks check-release --baseline "rev:$BASELINE" 2>&1 | tee /out/semver-report.txt
    echo "report saved to logs/semver-report.txt"
  '
