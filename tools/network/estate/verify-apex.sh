#!/usr/bin/env bash
# End-to-end proof that the auto.network front door is up and correct.
#
# Every acceptance criterion of bead auto-9q7a5 that can be observed from
# outside is checked here, and the check FAILS LOUDLY at the first rung that
# does not hold. Runs from anywhere with public network + dig/curl/openssl.
#
# Rungs:
#   1. DNS   — auto.network resolves to 5.161.219.195 from MULTIPLE public
#              resolvers (a single-resolver "yes" is the classic false success;
#              propagation is uneven, so we require agreement across several).
#   2. TLS   — Caddy serves a VALID certificate for auto.network (no -k).
#   3. health— https://auto.network/healthz → 200.
#   4. install content contract — /install is markdown to curl/default Accept
#              and self-contained HTML to a browser Accept, each with the
#              no-store / nosniff / no-referrer headers, and CSP on the HTML.
#   5. relay invariant — relay.auto.network still answers and the registry
#              still issues share links on relay.auto.network (NOT the apex).
#
# Usage:
#   ./verify-apex.sh                       # apex + relay reachability rungs
#   SMOKE_LINK=https://relay.auto.network/l/<token> ./verify-apex.sh
#                                          # also walks the full guest path and
#                                          # asserts the link host is relay
#
# Exit status is the point: 0 means every rung that ran passed.

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

APEX=auto.network
APEX_IP=5.161.219.195
RELAY=relay.auto.network
REGISTRY=registry.auto.network
RESOLVERS=(1.1.1.1 8.8.8.8 9.9.9.9 208.67.222.222)

PY=python3
for c in python3.13 python3.12 python3.11 python3; do
    command -v "$c" >/dev/null 2>&1 && { PY=$c; break; }
done

fail=0
ok()   { printf '  ok    %s\n' "$1"; }
bad()  { printf '  FAIL  %s\n' "$1"; fail=1; }

echo "== rung 1: DNS across ${#RESOLVERS[@]} public resolvers =="
agree=0
for r in "${RESOLVERS[@]}"; do
    got=$(dig +short @"$r" "$APEX" A 2>/dev/null | tail -n1)
    if [ "$got" = "$APEX_IP" ]; then
        ok "@$r → $got"
        agree=$((agree + 1))
    else
        bad "@$r → ${got:-<none>} (want $APEX_IP)"
    fi
done
# Require agreement from the MAJORITY of resolvers — one lucky hit is not proof.
if [ "$agree" -lt 3 ]; then
    bad "only $agree/${#RESOLVERS[@]} resolvers agree on $APEX_IP — not converged"
fi

echo "== rung 2: valid TLS certificate for $APEX (no -k) =="
# curl without -k fails on an invalid/absent cert; that IS the assertion.
if curl -sS --max-time 20 -o /dev/null "https://$APEX/healthz"; then
    ok "curl completed TLS handshake without -k"
else
    bad "TLS handshake to https://$APEX failed (cert not yet issued or invalid)"
fi
# Confirm the served cert actually names the apex.
cn=$(printf '' | openssl s_client -servername "$APEX" -connect "$APEX:443" 2>/dev/null \
     | openssl x509 -noout -subject -ext subjectAltName 2>/dev/null)
if printf '%s' "$cn" | grep -qF "$APEX"; then
    ok "certificate covers $APEX"
else
    bad "certificate does not name $APEX (got: ${cn:-<none>})"
fi

echo "== rung 3: https://$APEX/healthz → 200 =="
code=$(curl -sS --max-time 20 -o /dev/null -w '%{http_code}' "https://$APEX/healthz" 2>/dev/null)
[ "$code" = 200 ] && ok "/healthz → 200" || bad "/healthz → ${code:-<none>}"

echo "== rung 4: /install content contract =="
"$PY" - "$APEX" <<'PY'
import sys, urllib.request, urllib.error

host = sys.argv[1]
url = f"https://{host}/install"

def fetch(accept):
    req = urllib.request.Request(url, headers={"Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()

rc = 0
def check(name, cond):
    global rc
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}")
    if not cond:
        rc = 1

# curl / default Accept → markdown
st, h, body = fetch("*/*")
ct = h.get("content-type", "")
check("curl Accept → 200", st == 200)
check("curl Accept → text/markdown", "text/markdown" in ct)
check("curl Accept → not HTML-wrapped", b"<!DOCTYPE html>" not in body[:64])
check("markdown: cache-control no-store", h.get("cache-control") == "no-store")
check("markdown: x-content-type-options nosniff", h.get("x-content-type-options") == "nosniff")
check("markdown: referrer-policy no-referrer", h.get("referrer-policy") == "no-referrer")

# browser Accept → self-contained HTML
st, h, body = fetch("text/html,application/xhtml+xml")
ct = h.get("content-type", "")
check("browser Accept → 200", st == 200)
check("browser Accept → text/html", "text/html" in ct)
check("browser Accept → self-contained HTML doc", body.lstrip().startswith(b"<!DOCTYPE html>"))
# Self-contained = no external subresource fetches: no <script src>/<link>/
# <img src> pointing off-box. URL strings inside the embedded primer text are
# fine — they are content, not fetches. So check the subresource *shape*, and
# the CSP that structurally forbids external origins at the browser.
check("browser Accept → no external <script src>", b'src="http' not in body.lower())
check("browser Accept → no <link> subresource", b"<link" not in body.lower())
check("html: cache-control no-store", h.get("cache-control") == "no-store")
check("html: x-content-type-options nosniff", h.get("x-content-type-options") == "nosniff")
check("html: referrer-policy no-referrer", h.get("referrer-policy") == "no-referrer")
csp = h.get("content-security-policy", "")
check("html: content-security-policy present", bool(csp))
check("html: CSP default-src 'none' (no external origins)", "default-src 'none'" in csp)

sys.exit(rc)
PY
[ $? -eq 0 ] || fail=1

echo "== rung 5: relay invariant (share links stay on $RELAY) =="
for host in "$RELAY" "$REGISTRY"; do
    code=$(curl -sS --max-time 20 -o /dev/null -w '%{http_code}' "https://$host/healthz" 2>/dev/null)
    [ "$code" = 200 ] && ok "$host/healthz → 200" || bad "$host/healthz → ${code:-<none>}"
done
# The registry deploy ships a smoke test that walks the guest path. Reuse it so
# "a real relay share-link smoke remains green" is proven by the same code the
# service deploy uses — and, with SMOKE_LINK set, that the link host is relay.
SMOKE=../registry/deploy/smoke.py
if [ -f "$SMOKE" ]; then
    if [ -n "${SMOKE_LINK:-}" ]; then
        case "$SMOKE_LINK" in
        https://"$RELAY"/*) ok "SMOKE_LINK host is $RELAY (not the apex)" ;;
        *) bad "SMOKE_LINK is not on $RELAY: $SMOKE_LINK" ;;
        esac
        "$PY" "$SMOKE" "https://$RELAY" --link "$SMOKE_LINK" || fail=1
    else
        "$PY" "$SMOKE" "https://$RELAY" || fail=1
        echo "  note: set SMOKE_LINK=https://$RELAY/l/<token> to prove the full guest path"
    fi
else
    echo "  note: $SMOKE not found; ran reachability only"
fi

echo
if [ "$fail" -eq 0 ]; then
    echo "ALL RUNGS PASSED — auto.network front door is up and share links stay on $RELAY"
else
    echo "ONE OR MORE RUNGS FAILED — front door is not proven; see above" >&2
fi
exit "$fail"
