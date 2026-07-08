# Shared plumbing for the jira-* tools. Sourced, not executed.
#
# These tools hold NO Jira credential. Reads call the dashboard's broker
# routes (the Jira call runs host-side); writes stage an approval request
# (kind=jira_write) and block until the operator decides — on approval the
# dashboard executes the write host-side and the outcome comes back through
# the approval result.

DASH="${AUTONOMY_DASHBOARD:-${GRAPH_API:-https://localhost:8080}}"
SESSION="${AUTONOMY_SESSION:-}"

jira_fail() { echo "$1" >&2; exit 1; }

# jira_read_body: -f FILE, a positional file, or stdin -> prints the body.
jira_read_body() {
  if [ "${1:-}" = "-f" ]; then
    [ -n "${2:-}" ] && [ -f "$2" ] || jira_fail "usage: -f FILE (file not found: ${2:-})"
    cat "$2"
  elif [ -n "${1:-}" ] && [ -f "$1" ]; then
    cat "$1"
  else
    cat
  fi
}

# jira_approval_wait REQUEST_JSON_FILE — stage the approval, block on the
# held GET until the operator decides, print the execution outcome JSON.
# Exit 1 with a clear message on decline or execution failure.
jira_approval_wait() {
  local req_file="$1" id state
  [ -n "$SESSION" ] || jira_fail "jira: AUTONOMY_SESSION is not set"
  id="$(curl -sk -X POST "$DASH/api/approvals" -H 'Content-Type: application/json' \
          --data-binary @"$req_file" \
          | python3 -c 'import sys,json; print(json.load(sys.stdin).get("id",""))' 2>/dev/null)"
  [ -n "$id" ] || jira_fail "jira: dashboard did not accept the request (is it running at $DASH?)"
  echo "Waiting for operator approval (request $id)…" >&2
  while :; do
    state="$(curl -sk --max-time 70 "$DASH/api/approvals/$id?wait=55" \
              | python3 -c 'import sys,json
r=json.load(sys.stdin).get("result")
if r is None: print("PENDING")
elif not r.get("approved"): print("DECLINED")
else:
    ex=r.get("execution") or {}
    print("DONE" if ex.get("ok") else "FAILED\t"+str(ex.get("error","no execution outcome")))' 2>/dev/null)"
    case "$state" in
      DONE)     break ;;
      DECLINED) jira_fail "Operator declined the Jira write. Confirm intent with the user before retrying." ;;
      FAILED*)  jira_fail "jira: approved but execution failed: ${state#FAILED	}" ;;
      PENDING)  ;;          # held call elapsed undecided — hold a fresh one
      *)        sleep 2 ;;  # network/parse hiccup — brief backoff
    esac
  done
  curl -sk "$DASH/api/approvals/$id?wait=0" \
    | python3 -c 'import sys,json; print(json.dumps(json.load(sys.stdin)["result"]["execution"], indent=2))'
}
