#!/usr/bin/env bash
# Declare that this worktree's PR was amended/pushed and arm the
# Worktrees dashboard's smart-cadence terminal nag.
#
# Behaviour:
#   1. Force-refreshes the dashboard's source_control snapshot for the
#      row (binding-walk + REST cache write — same as clicking Refresh).
#   2. Arms ``nag_when_terminal`` with a 2-hour cap. The dashboard polls
#      on a smart cadence (30s / 60s / 5min tiers) and fires one
#      CrossTalk per PR when its checks transition to terminal — GREEN
#      with the count of passing checks, or RED with the failing names.
#
# Usage (after ``git push`` / ``git push --force-with-lease``):
#   declare-pr-amended.sh
#   declare-pr-amended.sh <SESSION> <REPO>
#
# Required env (set automatically by the dispatcher in agent containers):
#   SESSION_NAME — e.g. ``auto-x``
#   REPO_NAME    — e.g. ``autonomy``
#   DASHBOARD_URL — defaults to ``https://localhost:8080``
set -euo pipefail

usage() {
  sed -n '2,18p' "$0" >&2
  exit 64
}

if [ $# -ge 1 ]; then
  case "$1" in
    -h|--help) usage ;;
  esac
fi

SESSION="${1:-${SESSION_NAME:-}}"
REPO="${2:-${REPO_NAME:-}}"
: "${SESSION:?SESSION_NAME (or first arg) is required}"
: "${REPO:?REPO_NAME (or second arg) is required}"

DASHBOARD_URL="${DASHBOARD_URL:-https://localhost:8080}"

# ``-sk`` because the dashboard runs HTTPS with a self-signed cert
# (graph://d970d946-f95). ``--fail-with-body`` makes a 4xx/5xx response
# both exit non-zero AND print the body to stderr so the agent sees
# "worktree not found" instead of a silent success.
exec curl -sk --fail-with-body -X POST \
  "${DASHBOARD_URL}/api/worktrees/${SESSION}/${REPO}/refresh?nag_when_terminal=1"
