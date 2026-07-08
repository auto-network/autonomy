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

# git also invokes gpg.program to VERIFY signatures (git verify-commit, %G?,
# log --show-signature, and verify steps inside some rebase/merge ops). We only
# sign; hand verification to real gpg so a verify never POSTs a phantom
# sign-request or hangs.
for _arg in "$@"; do
  case "$_arg" in
    --verify) exec gpg "$@" ;;
  esac
done

# Reach the dashboard at the same URL every other call in this container uses:
# GRAPH_API (host.docker.internal on the bridge network, localhost on host net).
DASH="${AUTONOMY_DASHBOARD:-${GRAPH_API:-https://localhost:8080}}"
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

# Enforce the sign-off trailer when the policy requires it (signoff_and_gpg).
# The commit-msg hook normally adds it automatically; this is the un-bypassable
# backstop (a hook is skipped by --no-verify, the shim is not). Fail BEFORE
# posting so the operator isn't asked to sign a commit that will fail DCO.
if [ "$(git config --get autonomy.sign.requireSignoff 2>/dev/null || true)" = "true" ]; then
  grep -qiE '^Signed-off-by: .+' "$payload" \
    || fail "commit signing: policy requires a Signed-off-by trailer and this commit has none. Re-commit with --signoff (the commit-msg hook adds it automatically unless you used --no-verify)."
fi

# create the approval request (kind=commit_sign): the exact commit bytes ride
# base64-encoded inside the kind-specific request JSON.
req="$(mktemp)" || fail "commit signing: could not create temp file"
trap 'rm -f "$payload" "$req"' EXIT
python3 - "$SESSION" "$REPO" "$payload" > "$req" <<'PY' || fail "commit signing: could not build the request"
import base64, json, sys
session, repo, path = sys.argv[1:4]
print(json.dumps({"kind": "commit_sign", "session": session, "request": {
    "repo": repo,
    "payload_b64": base64.b64encode(open(path, "rb").read()).decode("ascii"),
}}))
PY
id="$(curl -sk -X POST "$DASH/api/approvals" -H 'Content-Type: application/json' \
        --data-binary @"$req" \
        | python3 -c 'import sys,json; print(json.load(sys.stdin).get("id",""))' 2>/dev/null)"
[ -n "$id" ] || fail "commit signing: dashboard did not accept the request (is it running at $DASH?)"

# block until the operator decides. No polling: ?wait= makes the server hold
# the GET open until the decision is written (or the window elapses, in which
# case we immediately hold a fresh one). result null = still pending, approved
# with a signature = signed, approved without one = dashboard error, otherwise
# declined.
while :; do
  state="$(curl -sk --max-time 70 "$DASH/api/approvals/$id?wait=55" \
            | python3 -c 'import sys,json
r=json.load(sys.stdin).get("result")
if r is None: print("PENDING")
elif not r.get("approved"): print("DECLINED")
elif r.get("signature"): print("SIGNED")
else: print("ERROR")' 2>/dev/null)"
  case "$state" in
    SIGNED)   break ;;
    DECLINED) fail "User declined signing request, confirm with user their intent." ;;
    ERROR)    fail "commit signing: the request was approved but no signature came back (dashboard error)." ;;
    PENDING)  ;;          # held call elapsed undecided — hold a fresh one
    *)        sleep 2 ;;  # network/parse hiccup (e.g. dashboard restart) — brief backoff
  esac
done

# ?wait=0: decided request returns immediately on the bare (no-enrichment) path
sig="$(curl -sk "$DASH/api/approvals/$id?wait=0" \
        | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["signature"])')"

# hand git the armored signature (stdout) + the status line it looks for (stderr)
echo "[GNUPG:] SIG_CREATED D" >&2
printf '%s\n' "$sig"
