#!/usr/bin/env bash
# Declare an ``autonomy.worktree.review_binding#1`` Setting for the
# current worktree row + a freshly-created PR. Run this immediately
# after ``gh pr create``.
#
# Usage:
#   declare-review-binding.sh
#   declare-review-binding.sh <REVIEW_ID>
#   declare-review-binding.sh --previous-review-id <PREV_ID> <REVIEW_ID>
#   declare-review-binding.sh --base-sha <SHA> <REVIEW_ID>
#
# Without ``REVIEW_ID`` the helper resolves the current branch's PR via
# ``gh pr view``. Without ``--previous-review-id`` / ``--base-sha`` it
# prefers that PR's ``baseRefOid`` from GitHub — the correct base commit
# even when the PR is stacked on another branch. If GitHub does not return
# ``baseRefOid``, the helper falls back to ``git merge-base origin/main
# HEAD``. ``--previous-review-id`` still chains the binding off the
# previous PR's head_sha (read from the cache via ``graph set get``).
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

REVIEW_ID="${1:-}"

: "${SESSION_NAME:?SESSION_NAME must be set (dispatcher injects it; export manually for local runs)}"
: "${REPO_NAME:?REPO_NAME must be set}"
BRANCH="${BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"

GH_PR_JSON=""
if [ -z "$REVIEW_ID" ] || { [ -z "$EXPLICIT_BASE_SHA" ] && [ -z "$PREVIOUS_REVIEW_ID" ]; }; then
  GH_PR_JSON="$(gh pr view ${REVIEW_ID:+$REVIEW_ID} --json number,baseRefOid)"
fi

if [ -z "$REVIEW_ID" ]; then
  REVIEW_ID="$(printf '%s' "$GH_PR_JSON" | jq -r '.number // empty')"
  if [ -z "$REVIEW_ID" ]; then
    echo "could not determine REVIEW_ID from gh pr view" >&2
    exit 64
  fi
fi

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
elif [ -n "$GH_PR_JSON" ]; then
  BASE_SHA="$(printf '%s' "$GH_PR_JSON" | jq -r '.baseRefOid // empty')"
  if [ -z "$BASE_SHA" ]; then
    BASE_SHA="$(git merge-base origin/main HEAD)"
  fi
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
