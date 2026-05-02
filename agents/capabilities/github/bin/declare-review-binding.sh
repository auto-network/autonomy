#!/usr/bin/env bash
# Declare an ``autonomy.worktree.review_binding#1`` Setting for the
# current worktree row + a freshly-created PR. Run this immediately
# after ``gh pr create``.
#
# Usage:
#   declare-review-binding.sh <REVIEW_ID>
#   declare-review-binding.sh --previous-review-id <PREV_ID> <REVIEW_ID>
#   declare-review-binding.sh --base-sha <SHA> <REVIEW_ID>
#
# Without ``--previous-review-id`` / ``--base-sha`` the helper computes
# the binding's ``base_sha`` as ``git merge-base origin/main HEAD`` —
# the right value for a non-stacked PR. ``--previous-review-id`` chains
# the binding off the previous PR's head_sha (read from the cache via
# ``graph set get``), giving stacked PRs the per-PR-scoped diff the
# UI expects.
#
# Required env (set automatically by the dispatcher in agent containers):
#   SESSION_NAME — e.g. ``auto-x``
#   REPO_NAME    — e.g. ``autonomy``
#   BRANCH       — e.g. ``session/auto-x`` (defaults to current branch)
set -euo pipefail

usage() {
  sed -n '2,18p' "$0" >&2
  exit 64
}

PREVIOUS_REVIEW_ID=""
EXPLICIT_BASE_SHA=""

while [ $# -gt 0 ]; do
  case "$1" in
    --previous-review-id)
      PREVIOUS_REVIEW_ID="${2:-}"
      shift 2
      ;;
    --base-sha)
      EXPLICIT_BASE_SHA="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    --*)
      echo "unknown option: $1" >&2
      usage
      ;;
    *)
      break
      ;;
  esac
done

if [ $# -lt 1 ]; then
  echo "missing REVIEW_ID" >&2
  usage
fi

REVIEW_ID="$1"

: "${SESSION_NAME:?SESSION_NAME must be set (dispatcher injects it; export manually for local runs)}"
: "${REPO_NAME:?REPO_NAME must be set}"
BRANCH="${BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"

if [ -n "$EXPLICIT_BASE_SHA" ]; then
  BASE_SHA="$EXPLICIT_BASE_SHA"
elif [ -n "$PREVIOUS_REVIEW_ID" ]; then
  # Read the previous PR's cached head_sha from the review_state cache.
  # ``graph set get`` returns the JSON payload; jq plucks head_sha.
  REPO_SLUG="$(git config --get remote.origin.url \
    | sed -E 's#.*[:/]([^/]+/[^/]+?)(\.git)?$#\1#')"
  CACHE_KEY="${REPO_SLUG}:${PREVIOUS_REVIEW_ID}"
  PREV_HEAD="$(graph set get \
    'autonomy.source_control.review_state#1' \
    --key "$CACHE_KEY" --org autonomy 2>/dev/null \
    | jq -r '.head_sha // empty')"
  if [ -z "$PREV_HEAD" ]; then
    echo "could not read previous PR head_sha from cache for ${CACHE_KEY}" >&2
    echo "run a force-refresh against the previous PR first, then retry" >&2
    exit 65
  fi
  BASE_SHA="$PREV_HEAD"
else
  BASE_SHA="$(git merge-base origin/main HEAD)"
fi

if [ -z "$BASE_SHA" ]; then
  echo "could not determine base_sha — aborting" >&2
  exit 66
fi

KEY="${SESSION_NAME}:${REPO_NAME}:${BRANCH}:${REVIEW_ID}"
PAYLOAD="$(jq -cn --arg base_sha "$BASE_SHA" '{base_sha: $base_sha}')"

graph set add 'autonomy.worktree.review_binding#1' \
  --key "$KEY" \
  --inline "$PAYLOAD" \
  --org autonomy

echo "declared binding: ${KEY} -> base_sha=${BASE_SHA}"
