#!/usr/bin/env bash
# Commit-signing shim — used as git's `gpg.program`.
#
# When a workspace's commit policy requires a GPG signature, worktree setup points
# `gpg.program` at this script and sets `commit.gpgSign true`. git then invokes it
# for every commit as:  commit_sign_shim.sh --status-fd=2 -bsau <keyid>  with the
# commit payload on stdin. This routes the signing to the dashboard: the operator
# reviews the commit and signs it in their browser; git never sees the key.
#
# It posts the exact commit bytes to the rendezvous, blocks until the operator
# signs (or cancels), then hands git back the armored signature plus the one
# status line git needs. On cancel it exits non-zero with a message git surfaces
# to the agent. No timeout: the commit waits until the operator acts.

set -uo pipefail

DASH="${AUTONOMY_DASHBOARD:-https://localhost:8080}"
# session + repo are written into the worktree's git config by worktree setup;
# fall back to the session env var for session.
SESSION="$(git config --get autonomy.sign.session 2>/dev/null || true)"
[ -z "$SESSION" ] && SESSION="${AUTONOMY_SESSION:-}"
REPO="$(git config --get autonomy.sign.repo 2>/dev/null || true)"

fail() { echo "$1" >&2; exit 1; }

payload="$(mktemp)" || fail "commit signing: could not create temp file"
trap 'rm -f "$payload"' EXIT
cat > "$payload"   # the EXACT commit bytes (no $(...) stripping)

[ -s "$payload" ] && [ -n "$SESSION" ] || fail "commit signing: missing payload or session"

# create the sign-request: session/repo as url-encoded query params, the exact
# commit bytes as the POST body.
_enc() { python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }
url="$DASH/api/sign-requests?session=$(_enc "$SESSION")&repo=$(_enc "$REPO")"
id="$(curl -sk -X POST "$url" --data-binary @"$payload" \
        | python3 -c 'import sys,json; print(json.load(sys.stdin).get("id",""))' 2>/dev/null)"
[ -n "$id" ] || fail "commit signing: dashboard did not accept the request (is it running at $DASH?)"

# block until the operator signs (armored) or cancels (empty string)
while :; do
  state="$(curl -sk "$DASH/api/sign-requests/$id" \
            | python3 -c 'import sys,json
v=json.load(sys.stdin).get("signature")
print("PENDING" if v is None else ("DECLINED" if v=="" else "SIGNED"))' 2>/dev/null)"
  case "$state" in
    SIGNED)   break ;;
    DECLINED) fail "User declined signing request, confirm with user their intent." ;;
    *)        sleep 3 ;;
  esac
done

sig="$(curl -sk "$DASH/api/sign-requests/$id" \
        | python3 -c 'import sys,json; print(json.load(sys.stdin)["signature"])')"

# hand git the armored signature (stdout) + the status line it looks for (stderr)
echo "[GNUPG:] SIG_CREATED D" >&2
printf '%s\n' "$sig"
