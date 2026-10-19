#!/usr/bin/env bash
# findings-to-pr.sh — accepted tool findings -> sandboxed agent fix -> verified PR.
#
# The ONLY place the agent is allowed to work is a fresh clone under
# /home/adi/sandbox/. It never touches the live checkout, walton, backups, or
# .env — a host-side integrity manifest (including .env atime) proves it.
#
# Gates before a PR exists:
#   1. full unit suite green in the sandbox
#   2. CORE contract tests green — a core failure means a main-app entity was
#      changed: ESCALATION to a human, no auto-PR
#   3. red->green proof: every test_fix_* the agent added FAILS on the base
#      commit and PASSES on the branch (a test that passes both ways proves nothing)
#   4. verifier agent reproduces the original finding scenario -> FIXED/STILL BROKEN
#   5. integrity manifest unchanged outside the sandbox
#
# The agent never pushes and never opens PRs — this script does, after gates.
# The agent never reads or writes .env; config needs go in the PR description
# under "Proposed .env additions".
#
# Usage:
#   scripts/findings-to-pr.sh            # run against accepted findings
#   scripts/findings-to-pr.sh --dry-run  # show findings, build nothing
#
# Everything is overridable per machine (defaults are portable):
#   CODEGRAPH_REPO       repo to fix (default: the repo this script lives in)
#   CODEGRAPH_SANDBOX    where disposable clones live (default: ~/.cache/codegraph-autofix)
#   CODEGRAPH_AGENT_BIN  coding agent CLI (default: opencode — also: claude, …)
#   CODEGRAPH_AGENT_ARGS args before the prompt (default: 'run' — claude: '-p')
#   CODEGRAPH_PROTECTED_PATHS  colon-separated paths the manifest tracks
#                        (default: repo, repo/.env, repo/backups, ~/.ssh)
#   CODEGRAPH_CODEBASE_ROOT    extra protected path: the analyzed codebase
#   BACKEND_CONTAINER    docker container for findings/email (default: oncall-backend)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_REPO="${CODEGRAPH_REPO:-$(dirname "$SCRIPT_DIR")}"
SANDBOX_ROOT="${CODEGRAPH_SANDBOX:-${XDG_CACHE_HOME:-$HOME/.cache}/codegraph-autofix}"
AGENT_BIN="${CODEGRAPH_AGENT_BIN:-opencode}"
AGENT_ARGS="${CODEGRAPH_AGENT_ARGS:-run}"
BACKEND_CONTAINER="${BACKEND_CONTAINER:-oncall-backend}"
RUN_DATE="$(date +%F)"
BRANCH="autofix/${RUN_DATE}"
RUN_DIR="$SANDBOX_ROOT/findings-run-${RUN_DATE}-$$"
MAX_AGENT_ITERATIONS=3
CORE_TEST_FILTER='Contract or Snapshot or PowerEvent or TokenBucket or SystemOneGate or BoostWeight or Idempotency or registry_pinned or AuthPath or ToolsDoc'

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }

# ---------------------------------------------------------------------------
# 0. Locate things (gh is optional — see PR step for the fallback)
# ---------------------------------------------------------------------------
command -v "$AGENT_BIN" >/dev/null || die "agent CLI '$AGENT_BIN' not on PATH (set CODEGRAPH_AGENT_BIN)"
[ -d "$REAL_REPO/.git" ] || die "real repo not found at $REAL_REPO"
ORIGIN_URL="$(git -C "$REAL_REPO" remote get-url origin)"
GH_BIN="$(command -v gh || true)"

run_agent() {
  # $1 = prompt (single argument — works for `opencode run` and `claude -p`)
  ( cd "$RUN_DIR/repo" && "$AGENT_BIN" $AGENT_ARGS "$1" )
}

# Evidence trail: LOCAL ONLY. logs/ is gitignored — nothing here ever
# reaches git. The sandbox itself is deleted on exit (see trap below).
EVIDENCE_DIR="$REAL_REPO/logs/autofix/$RUN_DATE"
mkdir -p "$EVIDENCE_DIR"

cleanup() {
  # Sandboxes are disposable — delete after every usage, success or failure.
  # Forensics live in $EVIDENCE_DIR (local, gitignored), never in the sandbox.
  if [ -n "${RUN_DIR:-}" ] && [ -d "$RUN_DIR" ]; then
    rm -rf "$RUN_DIR"
    log "Sandbox deleted: $RUN_DIR"
  fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 1. Integrity manifest — the "did the agent fuck around" proof.
#    .env is tracked by ATIME too: reading it is as forbidden as writing it.
# ---------------------------------------------------------------------------
IFS=':' read -r -a PROTECTED_PATHS <<< "${CODEGRAPH_PROTECTED_PATHS:-$REAL_REPO:$REAL_REPO/.env:$REAL_REPO/backups:$HOME/.ssh}"
if [ -n "${CODEGRAPH_CODEBASE_ROOT:-}" ]; then
  PROTECTED_PATHS+=("$CODEGRAPH_CODEBASE_ROOT")
fi
manifest() {
  # $1 = output file. Sandbox dir is the only place allowed to change.
  : > "$1"
  for p in "${PROTECTED_PATHS[@]}"; do
    [ -e "$p" ] || continue
    if [ -f "$p" ]; then
      stat --printf='%n mtime=%Y atime=%X size=%s\n' "$p" >> "$1"
    else
      find "$p" -xdev -printf='%p mtime=%T@ atime=%A@ size=%s\n' 2>/dev/null | sort >> "$1" || true
    fi
  done
  find "$SANDBOX_ROOT" -mindepth 1 -maxdepth 1 -not -name "$(basename "$RUN_DIR")" -printf='%p mtime=%T@\n' 2>/dev/null | sort >> "$1" || true
}

assert_manifest_unchanged() {
  local before="$1" after="$2"
  if ! diff -q "$before" "$after" >/dev/null 2>&1; then
    log "INTEGRITY VIOLATION — protected paths changed during the agent run:"
    diff "$before" "$after" | head -20 >&2 || true
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------------------
# 2. Pull accepted findings (orchestrator-only backend access)
# ---------------------------------------------------------------------------
collect_findings() {
  # Direct SQL against the live tables — stable across deploys (function
  # names drift between the running container and this script's vintage).
  docker exec -i "$BACKEND_CONTAINER" python - <<'PY'
import json
import psycopg
from psycopg.rows import dict_row
from app.core.config import settings

out = {"issues": [], "feedback": []}
try:
    with psycopg.connect(settings.postgres_dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """SELECT issue_type, summary, created_at FROM feedback_issues
                       WHERE created_at > NOW() - interval '30 days' ORDER BY created_at DESC"""
                )
                out["issues"] = [{"type": r["issue_type"], "summary": (r["summary"] or "")[:400], "when": str(r["created_at"])} for r in cur.fetchall()]
            except Exception:
                pass
            cur.execute(
                """SELECT feedback_id, pr_context, quality_rating, results_expected,
                          improvement_suggestions, created_at
                   FROM ai_feedback
                   WHERE status = 'accepted'
                     AND created_at > NOW() - interval '30 days'
                   ORDER BY created_at DESC LIMIT 100"""
            )
            for r in cur.fetchall():
                text = (r.get("improvement_suggestions") or "").strip()
                expected = (r.get("results_expected") or "").strip()
                if not text and not expected:
                    continue
                out["feedback"].append({
                    "id": (r.get("feedback_id") or "")[:8],
                    "pr_context": r.get("pr_context") or "",
                    "rating": r.get("quality_rating"),
                    "expected": expected[:800],
                    "suggestions": text[:1200],
                })
except Exception as exc:
    out["error"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(out))
PY
}

log "Collecting accepted findings..."
FINDINGS_JSON="$(collect_findings)" || die "could not collect findings"
FINDING_COUNT="$(printf '%s' "$FINDINGS_JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d.get("issues", [])) + len(d.get("feedback", [])))')"
log "Findings available: $FINDING_COUNT"
if [ "${1:-}" = "--dry-run" ]; then
  printf '%s\n' "$FINDINGS_JSON" | python3 -m json.tool | head -60
  exit 0
fi
if [ "$FINDING_COUNT" -eq 0 ]; then
  log "No findings — nothing to do."
  exit 0
fi

# ---------------------------------------------------------------------------
# 3. Sandbox: fresh clone, the agent's entire world
# ---------------------------------------------------------------------------
log "Sandbox: $RUN_DIR"
mkdir -p "$RUN_DIR"
git clone --quiet "$REAL_REPO" "$RUN_DIR/repo" || die "clone failed"
git -C "$RUN_DIR/repo" remote set-url --push origin "$ORIGIN_URL"
git -C "$RUN_DIR/repo" checkout --quiet -b "$BRANCH"
MANIFEST_BEFORE="$RUN_DIR/manifest.before"
manifest "$MANIFEST_BEFORE"

PR_BODY_DRAFT="$RUN_DIR/repo/PR_BODY.md"

# ---------------------------------------------------------------------------
# 4. Agent loop: fix -> host test gate -> feed failures back
# ---------------------------------------------------------------------------
read -r -d '' BUILDER_CONSTRAINTS <<EOF || true
You are fixing findings in THIS repository only (codegraph-mcp, the on-call graph MCP server). Your working directory IS the repository.

HARD RULES — violating any of these voids the work:
1. Edit and create files ONLY under the current working directory. Never touch any path outside this clone — no other repository exists for you, and protected paths ($(IFS=, ; echo "${PROTECTED_PATHS[*]}")) are tracked and off-limits.
2. NEVER read, write, print, grep, or reference .env or any secrets file — not even to 'check what is set'. If a fix needs a new env var or a changed value, do NOT edit .env; describe it in PR_BODY.md under a section titled "## Proposed .env additions".
3. Do NOT push, do NOT create branches, do NOT open PRs, do NOT run gh. Commit locally on the current branch only.
4. Do NOT modify or delete existing tests to make them pass. Existing tests encode core contracts of the main app. If an existing test seems wrong, say so in PR_BODY.md under "## Needs human verification" and leave it failing.
5. For EVERY finding you fix, add a regression test named test_fix_<short_slug> that REPRODUCES the reported behavior and fails without your fix. Put them in tests/unit/ (extend test_rag_and_mcp.py or add a new test_*.py module).
6. Run the suite with: ./scripts/run-tests.sh tests/unit -q   (run it from the repo root; it needs docker). Iterate until green.
7. Keep the diff minimal — fix the findings, nothing else. No drive-by refactors, no comment noise.

When done, write PR_BODY.md at the repo root with:
# Summary
(what you changed, in plain language)
## Findings addressed
(finding id/text -> how you fixed it)
## Regression tests
(test names and what each proves)
## Proposed .env additions
(yes/no — values never go here, just names and reasons)
## Needs human verification
(anything you could not fully fix or any contract tension)
EOF

ITER=1
TEST_OUTPUT=""
while [ "$ITER" -le "$MAX_AGENT_ITERATIONS" ]; do
  log "Agent iteration $ITER/$MAX_AGENT_ITERATIONS"
  if [ "$ITER" -eq 1 ]; then
    PROMPT="$(cat <<EOF
$BUILDER_CONSTRAINTS

--- FINDINGS TO FIX (verbatim from agents who used these tools) ---
$FINDINGS_JSON

Fix as many as you can properly. A partial fix with tests beats a broad fix without them.
EOF
)"
  else
    PROMPT="$(cat <<EOF
$BUILDER_CONSTRAINTS

Your previous attempt failed the test gate. Fix the failures without breaking anything else.

--- TEST OUTPUT ---
$TEST_OUTPUT
EOF
)"
  fi

  ( cd "$RUN_DIR/repo" && run_agent "$PROMPT" ) || log "agent exited non-zero (continuing to gates)"

  # --- Gate 1: full suite (host-enforced, this is the law) ---------------
  log "Gate 1/5: full unit suite"
  set +e
  TEST_OUTPUT="$( (cd "$RUN_DIR/repo" && timeout 600 ./scripts/run-tests.sh tests/unit -q) 2>&1 )"
  SUITE_RC=$?
  set -e
  if [ "$SUITE_RC" -eq 0 ]; then
    log "Suite green."
    break
  fi

  # --- Gate 2: core contracts? -------------------------------------------
  log "Suite failed — checking CORE contracts"
  set +e
  CORE_OUTPUT="$( (cd "$RUN_DIR/repo" && timeout 600 ./scripts/run-tests.sh tests/unit -q -k "$CORE_TEST_FILTER") 2>&1 )"
  CORE_RC=$?
  set -e
  if [ "$CORE_RC" -ne 0 ]; then
    log "CORE CONTRACT FAILURE — a main-app entity changed. Escalating to human."
    ESCALATION=1
    printf '%s' "$CORE_OUTPUT" > "$EVIDENCE_DIR/core-failure.log"
    break
  fi
  log "Core contracts intact — only non-core tests fail; feeding back to agent."
  ITER=$((ITER + 1))
done

# ---------------------------------------------------------------------------
# 5. Escalation path: core broken -> human, no auto-PR
# ---------------------------------------------------------------------------
send_mail() {
  local subject="$1" html="$2"
  docker exec "$BACKEND_CONTAINER" python -c "
from app.core.notifications import send_email
send_email('$subject', '''$html''')
" >/dev/null 2>&1 || log "email failed (non-fatal)"
}

if [ "${ESCALATION:-0}" = "1" ]; then
  MANIFEST_AFTER="$RUN_DIR/manifest.after"
  manifest "$MANIFEST_AFTER"
  INTEGRITY_OK=1
  assert_manifest_unchanged "$MANIFEST_BEFORE" "$MANIFEST_AFTER" || INTEGRITY_OK=0
  cp "$MANIFEST_BEFORE" "$MANIFEST_AFTER" "$EVIDENCE_DIR/" 2>/dev/null || true
  [ "$INTEGRITY_OK" = 1 ] || diff "$MANIFEST_BEFORE" "$MANIFEST_AFTER" > "$EVIDENCE_DIR/integrity-violation.diff" 2>/dev/null || true
  send_mail "[codegraph autofix] CORE contract failure — human verification required ($RUN_DATE)" \
    "<p>The autofix agent produced a change that breaks CORE contract tests — a main-app entity was modified and must be verified by hand.</p><pre>$(printf '%s' "$CORE_OUTPUT" | tail -30 | sed 's/</\&lt;/g')</pre><p>Evidence: <code>$EVIDENCE_DIR</code> (local only). Integrity: $( [ "$INTEGRITY_OK" = 1 ] && echo OK || echo 'VIOLATION — see manifest diff'). No PR was opened. Sandbox deleted.</p>"
  log "Escalation mail sent. Evidence at $EVIDENCE_DIR"
  exit 2
fi

# Gate 1 exit: suite never went green
if [ "$SUITE_RC" -ne 0 ]; then
  printf '%s' "$TEST_OUTPUT" > "$EVIDENCE_DIR/test-failure.log"
  send_mail "[codegraph autofix] findings run failed the test gate ($RUN_DATE)" \
    "<p>Agent iterations exhausted with a red suite. No PR opened. Evidence: <code>$EVIDENCE_DIR</code> (local only). Sandbox deleted.</p><pre>$(printf '%s' "$TEST_OUTPUT" | tail -30 | sed 's/</\&lt;/g')</pre>"
  exit 3
fi

# Nothing committed at all?
if [ "$(git -C "$RUN_DIR/repo" log --oneline main..HEAD | wc -l)" -eq 0 ] \
   && git -C "$RUN_DIR/repo" diff --quiet 2>/dev/null; then
  log "Agent produced no changes — exiting quietly."
  exit 0
fi

# ---------------------------------------------------------------------------
# 6. Red->green proof for every added test_fix_* -----------------------------
# ---------------------------------------------------------------------------
log "Gate 3/5: red->green proof for added regression tests"
BASE_SHA="$(git -C "$RUN_DIR/repo" rev-parse main)"
PROOF_ROWS=""
PROOF_OK=1
NEW_TESTS="$(git -C "$RUN_DIR/repo" diff main...HEAD | grep -E '^\+def test_fix_' | sed 's/^+def //; s/(.*//' || true)"
if [ -z "$NEW_TESTS" ]; then
  log "No test_fix_* regression tests added — a fix without a reproducing test is not accepted."
  PROOF_OK=0
  PROOF_ROWS="| (none) | MISSING | MISSING | — |"
else
  git -C "$RUN_DIR/repo" worktree add --quiet "$RUN_DIR/base" "$BASE_SHA"
  for t in $NEW_TESTS; do
    # copy the new/changed test files onto the base so the test exists there
    git -C "$RUN_DIR/repo" diff --name-only main...HEAD -- 'tests/*' | while read -r tf; do
      mkdir -p "$RUN_DIR/base/$(dirname "$tf")"
      cp "$RUN_DIR/repo/$tf" "$RUN_DIR/base/$tf"
    done
    set +e
    (cd "$RUN_DIR/base" && timeout 300 ./scripts/run-tests.sh tests/unit -q -k "$t") >/dev/null 2>&1
    BASE_RC=$?
    (cd "$RUN_DIR/repo" && timeout 300 ./scripts/run-tests.sh tests/unit -q -k "$t") >/dev/null 2>&1
    HEAD_RC=$?
    set -e
    BASE_MARK="FAILED(as expected)" ; [ "$BASE_RC" -ne 0 ] || { BASE_MARK="PASSED(no proof!)"; PROOF_OK=0; }
    HEAD_MARK="PASSED" ; [ "$HEAD_RC" -eq 0 ] || { HEAD_MARK="FAILED"; PROOF_OK=0; }
    PROOF_ROWS="${PROOF_ROWS}| \`$t\` | $BASE_MARK | $HEAD_MARK |$( [ "$BASE_RC" -ne 0 ] && [ "$HEAD_RC" -eq 0 ] && echo ' red→green OK' || echo ' PROOF REJECTED' )|\n"
  done
  git -C "$RUN_DIR/repo" worktree remove --force "$RUN_DIR/base" 2>/dev/null || true
fi

if [ "$PROOF_OK" != "1" ]; then
  printf '%b' "$PROOF_ROWS" > "$EVIDENCE_DIR/red-green-proof.md"
  send_mail "[codegraph autofix] red→green proof failed ($RUN_DATE)" \
    "<p>The agent's regression tests do not prove the fix (they pass on the base too, or are missing). No PR opened. Evidence: <code>$EVIDENCE_DIR</code> (local only). Sandbox deleted.</p>"
  exit 4
fi

# ---------------------------------------------------------------------------
# 7. Verifier agent — reproduce the original finding scenario ---------------
# ---------------------------------------------------------------------------
log "Gate 4/5: verifier agent re-reproduces each finding"
VERIFY_FILE="$RUN_DIR/repo/VERIFY.md"
VERIFY_PROMPT="$(cat <<EOF
You are a VERIFIER. Do not change any code. Working directory is the repo under test.

Here are findings that an agent claims to have fixed on this branch:
$FINDINGS_JSON

The diff of the fix is below. For EACH finding, reproduce the reporter's scenario against this branch: run the same tool/function call the finding describes (call the python function directly, or run a focused pytest, or read the code path) and judge whether the reported problem is gone.

Output ONLY a markdown file VERIFY.md at the repo root:
| Finding | Verdict | Evidence |
|---|---|---|
| <id or slug> | FIXED / STILL BROKEN / UNCERTAIN | <one line: the exact call you ran and its result> |

Verdicts must be honest. STILL BROKEN and UNCERTAIN are valuable answers.

--- DIFF (main...HEAD) ---
$(git -C "$RUN_DIR/repo" diff main...HEAD | head -400)
EOF
)"
( cd "$RUN_DIR/repo" && run_agent "$VERIFY_PROMPT" ) || log "verifier exited non-zero (continuing)"
[ -f "$VERIFY_FILE" ] || echo "| (all) | UNCERTAIN | verifier produced no report |" > "$VERIFY_FILE"

# ---------------------------------------------------------------------------
# 8. Integrity manifest after everything -------------------------------------
# ---------------------------------------------------------------------------
log "Gate 5/5: integrity manifest"
MANIFEST_AFTER="$RUN_DIR/manifest.after"
manifest "$MANIFEST_AFTER"
if ! assert_manifest_unchanged "$MANIFEST_BEFORE" "$MANIFEST_AFTER"; then
  diff "$MANIFEST_BEFORE" "$MANIFEST_AFTER" > "$EVIDENCE_DIR/integrity-violation.diff" 2>/dev/null || true
  send_mail "[codegraph autofix] INTEGRITY VIOLATION — sandbox escape ($RUN_DATE)" \
    "<p>The agent run modified or read protected paths outside its sandbox (repos, walton, backups, .env, or ssh). No PR opened. Investigate <code>$EVIDENCE_DIR</code> immediately.</p>"
  exit 5
fi
log "Integrity clean — nothing outside the sandbox was touched."

# ---------------------------------------------------------------------------
# 9. Ship: code-only commit, secret-scrubbed PR description ------------------
#    Agent artifacts (PR_BODY.md, VERIFY.md, manifests) NEVER enter git —
#    they are moved out of the clone before commit and live only in the local
#    evidence dir and the PR description.
# ---------------------------------------------------------------------------
log "Preparing code-only commit"
[ -f "$PR_BODY_DRAFT" ] && cp "$PR_BODY_DRAFT" "$EVIDENCE_DIR/PR_BODY.md" || true
[ -f "$VERIFY_FILE" ] && cp "$VERIFY_FILE" "$EVIDENCE_DIR/VERIFY.md" || true
rm -f "$RUN_DIR/repo/PR_BODY.md" "$RUN_DIR/repo/VERIFY.md" "$RUN_DIR/repo"/manifest.* "$RUN_DIR/repo"/findings*.json 2>/dev/null || true

git -C "$RUN_DIR/repo" add -A
git -C "$RUN_DIR/repo" reset -q -- PR_BODY.md VERIFY.md 2>/dev/null || true
git -C "$RUN_DIR/repo" commit -m "autofix: address tool findings ($RUN_DATE)" >/dev/null 2>&1 || true

PR_BODY="$(cat <<EOF
$( [ -f "$EVIDENCE_DIR/PR_BODY.md" ] && cat "$EVIDENCE_DIR/PR_BODY.md" || echo '(agent produced no PR_BODY.md)' )

---

## Verification evidence (generated)

### Red→green proof
| Test | On base (pre-fix) | On branch | Result |
|---|---|---|---|
$(printf '%b' "$PROOF_ROWS")

### Verifier agent verdicts
$(cat "$EVIDENCE_DIR/VERIFY.md" 2>/dev/null || cat "$VERIFY_FILE" 2>/dev/null || echo '—')

### Core contracts
All core contract tests pass — no main-app entity was changed (contract drift, snapshot round-trip, auth paths, rate limits, registry pin).

### Proposed .env additions
See agent section above. **Nothing under .env was read or written** — apply any proposal by hand after merge.

_Sandboxed run: the agent had write access only to a disposable clone (now deleted). Integrity manifest verified unchanged for the live checkout, walton, backups, .env (read-tracking included), and ssh keys._
EOF
)"

# Belt-and-braces: never let a secret-looking string into the PR description.
PR_BODY="$(printf '%s' "$PR_BODY" | python3 -c '
import re, sys
text = sys.stdin.read()
text = re.sub(r"(?i)((?:token|password|secret|api[_-]?key|passwd)\s*[=:]\s*)(\S+)", r"\1[REDACTED]", text)
text = re.sub(r"\b[A-Fa-f0-9]{40,}\b", "[REDACTED-HEX]", text)
text = re.sub(r"\bghp_[A-Za-z0-9]+\b|\bgithub_pat_[A-Za-z0-9_]+\b", "[REDACTED-TOKEN]", text)
sys.stdout.write(text)
')"
printf '%s' "$PR_BODY" > "$EVIDENCE_DIR/PR_BODY.scrubbed.md"

log "Pushing $BRANCH"
# Hooks are not cloned — install the real repo's secret-blocking pre-push
# guard so the sandbox push obeys the same rules as every other push.
[ -f "$REAL_REPO/.git/hooks/pre-push" ] && cp "$REAL_REPO/.git/hooks/pre-push" "$RUN_DIR/repo/.git/hooks/pre-push"
git -C "$RUN_DIR/repo" push --quiet -u origin "$BRANCH"

# PR creation: gh if installed, else GITHUB_TOKEN API, else a compare link
# the human clicks (body is in the email — paste and done).
PR_URL=""
COMPARE_URL="$(printf '%s' "$ORIGIN_URL" | sed 's#git@github.com:#https://github.com/#; s#\.git$##')/compare/main...${BRANCH}?expand=1"
if [ -n "$GH_BIN" ]; then
  "$GH_BIN" pr create --repo "$ORIGIN_URL" --base main --head "$BRANCH" \
    --title "autofix: tool findings — $RUN_DATE" --body "$PR_BODY" >/dev/null 2>&1 \
    && PR_URL="$("$GH_BIN" pr list --repo "$ORIGIN_URL" --head "$BRANCH" --json url --jq '.[0].url' 2>/dev/null)" || true
fi
if [ -z "$PR_URL" ] && [ -n "${GITHUB_TOKEN:-}" ]; then
  REPO_SLUG="$(printf '%s' "$ORIGIN_URL" | sed 's#git@github.com:##; s#https://github.com/##; s#\.git$##')"
  PR_URL="$(curl -sS -X POST "https://api.github.com/repos/$REPO_SLUG/pulls" \
    -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/vnd.github+json" \
    -d "{\"title\":\"autofix: tool findings — $RUN_DATE\",\"head\":\"$BRANCH\",\"base\":\"main\",\"body\":$(printf '%s' "$PR_BODY" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("html_url",""))' 2>/dev/null)" || true
fi

if [ -n "$PR_URL" ]; then
  log "PR open: $PR_URL"
  send_mail "[codegraph autofix] PR ready for review — $RUN_DATE" \
    "<p>Sandboxed autofix run produced a verified PR: <a href='$PR_URL'>$PR_URL</a></p><p>Gates: suite green · core contracts intact · red→green proof OK · verifier verdicts in PR body · integrity clean.</p><p>Review the diff and merge (or close) by hand — the agent has no push or merge rights. Evidence: <code>$EVIDENCE_DIR</code> (local only).</p>"
else
  log "No gh/GITHUB_TOKEN — pushed branch only, compare link in mail."
  send_mail "[codegraph autofix] branch pushed — open the PR by hand ($RUN_DATE)" \
    "<p>Sandboxed autofix run passed every gate and pushed <code>$BRANCH</code>, but this machine has no gh/GITHUB_TOKEN to open the PR itself.</p><p><a href='$COMPARE_URL'>Open a pre-filled PR here</a> and paste the body below.</p><p>Gates: suite green · core contracts intact · red→green proof OK · verifier verdicts · integrity clean. Evidence: <code>$EVIDENCE_DIR</code> (local only).</p><pre>$(printf '%s' "$PR_BODY" | sed 's/</\&lt;/g' | head -80)</pre>"
fi
log "Done. Sandbox deleted on exit; evidence in $EVIDENCE_DIR"
