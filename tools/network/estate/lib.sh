# Shared helpers for the auto.network estate scripts. Source, don't execute.
#
# Token resolution order:
#   1. $HCLOUD_TOKEN
#   2. $AUTO_NETWORK_TOKEN_FILE (path to a file containing the bare token)
#   3. the `auto-network` context in ~/.config/hcloud/cli.toml (workstation)
# The token is never written by these scripts; custody is the operator's
# (interim token, rotation planned — graph://8cb2a39c-4bc question 2).

API=https://api.hetzner.cloud/v1

# Token parsing needs `tomllib`, which is stdlib only from Python 3.11. The
# system python3 here is 3.10, so a bare `python3` makes every script in this
# directory die with "No module named 'tomllib'". Prefer the repo venv, then
# any newer interpreter on PATH, and fall back to python3 so the scripts still
# run anywhere the stdlib is new enough.
if [ -z "${ESTATE_PY:-}" ]; then
    for _py in \
        "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/.venv/bin/python" \
        python3.13 python3.12 python3.11 python3
    do
        if command -v "$_py" >/dev/null 2>&1 \
           && "$_py" -c 'import tomllib' >/dev/null 2>&1; then
            ESTATE_PY="$_py"
            break
        fi
    done
    unset _py
    : "${ESTATE_PY:=python3}"
fi

# Label every estate resource carries. Teardown refuses anything without it —
# the live mail pet (ubuntu-ash-1 / 58635516) shares this Hetzner project.
ESTATE_LABEL="managed-by=auto-network-estate"
PET_ID=58635516
PET_NAME=ubuntu-ash-1

resolve_token() {
    if [ -n "${HCLOUD_TOKEN:-}" ]; then
        return
    fi
    if [ -n "${AUTO_NETWORK_TOKEN_FILE:-}" ] && [ -f "$AUTO_NETWORK_TOKEN_FILE" ]; then
        HCLOUD_TOKEN=$(cat "$AUTO_NETWORK_TOKEN_FILE")
        return
    fi
    local toml="$HOME/.config/hcloud/cli.toml"
    if [ -f "$toml" ]; then
        HCLOUD_TOKEN=$("$ESTATE_PY" - "$toml" <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    cfg = tomllib.load(f)
for ctx in cfg.get("contexts", []):
    if ctx.get("name") == "auto-network":
        print(ctx.get("token", ""))
        break
PY
)
    fi
    if [ -z "${HCLOUD_TOKEN:-}" ]; then
        echo "no token: set HCLOUD_TOKEN, AUTO_NETWORK_TOKEN_FILE, or add an 'auto-network' hcloud context" >&2
        exit 1
    fi
}

# api METHOD PATH [json-body]  → response body on stdout, fails on HTTP error
api() {
    local method=$1 path=$2 body=${3:-}
    local args=(-sS --max-time 30 -X "$method" -H "Authorization: Bearer $HCLOUD_TOKEN")
    if [ -n "$body" ]; then
        args+=(-H "Content-Type: application/json" -d "$body")
    fi
    local resp
    resp=$(curl "${args[@]}" "$API$path")
    if [ "$(printf '%s' "$resp" | "$ESTATE_PY" -c 'import json,sys; print("err" if "error" in json.load(sys.stdin) else "ok")' 2>/dev/null)" = "err" ]; then
        printf '%s\n' "$resp" | "$ESTATE_PY" -c 'import json,sys; e=json.load(sys.stdin)["error"]; print(f"hcloud API error: {e[\"code\"]}: {e[\"message\"]}", file=sys.stderr)'
        return 1
    fi
    printf '%s' "$resp"
}

json_get() { # json_get '<python expr over d>'  — reads JSON on stdin
    "$ESTATE_PY" -c "import json,sys; d=json.load(sys.stdin); print($1)"
}
