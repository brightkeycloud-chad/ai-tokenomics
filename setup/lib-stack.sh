#!/usr/bin/env bash
#
# Shared functions for deploy-stack.sh and teardown-stack.sh. Not run directly.
#
# Settings (environment variables):
#   OWNER       GitHub user or organization   default: the signed-in gh user
#   APP_REPO    application repository        default: tokenomics-status-board-app
#   INFRA_REPO  infrastructure repository     default: tokenomics-status-board-infra

set -euo pipefail

for tool in gh aws curl python3; do
  command -v "$tool" >/dev/null || { echo "error: $tool is not installed" >&2; exit 1; }
done
gh auth status >/dev/null 2>&1 || { echo "error: gh is not signed in; run: gh auth login" >&2; exit 1; }

OWNER="${OWNER:-$(gh api user --jq .login)}"
APP_REPO="${APP_REPO:-tokenomics-status-board-app}"
INFRA_REPO="${INFRA_REPO:-tokenomics-status-board-infra}"
INFRA="$OWNER/$INFRA_REPO"
APP="$OWNER/$APP_REPO"

START_SECONDS=$SECONDS

say() { printf '\n== %s\n' "$*"; }
fail() { echo "error: $*" >&2; exit 1; }
elapsed() { local s=$((SECONDS - START_SECONDS)); printf '%dm %02ds' $((s / 60)) $((s % 60)); }

# The value of a repository variable, or empty.
repo_var() { gh variable get "$2" --repo "$1" 2>/dev/null || true; }

need_repo_vars() {
  local repo="$1"; shift
  gh repo view "$repo" >/dev/null 2>&1 || fail "repository $repo not found. Run setup/publish-workloads.sh."
  local name missing=0
  for name in "$@"; do
    if [ -z "$(repo_var "$repo" "$name")" ]; then
      echo "error: repository variable $name is not set on $repo" >&2
      missing=1
    fi
  done
  [ "$missing" = 0 ] || fail "run setup/configure-repos.sh first"
  [ "$(gh api "repos/$repo/actions/permissions" --jq .enabled)" = "true" ] \
    || fail "GitHub Actions is disabled on $repo. Run setup/configure-repos.sh first."
}

latest_run() {
  gh run list --repo "$1" --workflow "$2" --limit 1 --json databaseId --jq '.[0].databaseId // empty'
}

# Wait until no run of this workflow is queued or in progress, so a new run
# does not queue behind an old one without notice.
wait_idle() {
  local repo="$1" workflow="$2" busy
  while :; do
    busy=$(gh run list --repo "$repo" --workflow "$workflow" --limit 5 \
      --json status --jq '[.[] | select(.status != "completed")] | length')
    [ "$busy" = 0 ] && return
    echo "   a $workflow run is still active in $repo; waiting"
    sleep 15
  done
}

# Start a workflow, find the new run, and follow it to the end.
# Usage: run_and_wait <repo> <workflow> [gh workflow run options]
run_and_wait() {
  local repo="$1" workflow="$2" before run tries=0
  shift 2
  wait_idle "$repo" "$workflow"
  before=$(latest_run "$repo" "$workflow")
  gh workflow run "$workflow" --repo "$repo" "$@" >/dev/null
  # gh workflow run returns before the run exists. Wait for a new run ID.
  run=$before
  until [ -n "$run" ] && [ "$run" != "$before" ]; do
    tries=$((tries + 1))
    [ "$tries" -le 40 ] || fail "no new $workflow run appeared in $repo after 2 minutes"
    sleep 3
    run=$(latest_run "$repo" "$workflow")
  done
  echo "   run: https://github.com/$repo/actions/runs/$run"
  if ! gh run watch "$run" --repo "$repo" --exit-status --interval 10 >/dev/null 2>&1; then
    echo "   the run failed. The failed steps:" >&2
    gh run view "$run" --repo "$repo" --log-failed 2>/dev/null | tail -n 25 >&2 || true
    fail "$workflow failed in $repo (run $run)"
  fi
  echo "   $workflow succeeded ($(elapsed) since start)"
}
