#!/usr/bin/env bash
# Build an estate site in one command: provision the VM, wait for it, ship
# the registry code, and start the DNS responder — the whole
# automatable span, chained and idempotent. The cattle test: run this
# against a fresh location and see exactly how far pure automation gets
# before a real gate (a human or DNS) is required.
#
# Usage:
#   build-site.sh <name> [--location hil] [--type cpx11] [--public-edge]
#
# What it does (each step idempotent; re-running is safe):
#   1. provision-vm.sh <name> --role service --location <loc>  -> IP
#   2. wait for SSH + cloud-init to finish
#   3. registry/deploy/deploy.sh root@IP        (code, incl. DNS responder)
#   4. dns/deploy.sh --host root@IP             (DNS process on :53)
#   5. --public-edge only: deploy-caddy.sh root@IP --bind-ip IP
#
# The two DELIBERATE hand-offs pure automation cannot own, printed at the
# end so the pet/cattle boundary is explicit, never hidden:
#   * Public Caddy edge (step 5) needs DNS already routing the box's
#     hostnames to it, or Let's Encrypt HTTP-01 fails and burns rate
#     limit. Skipped unless --public-edge AND the box is in the anycast /
#     DNS set. A standalone proof box serves DNS + a loopback registry
#     without a public TLS edge — that is correct, not incomplete.
#   * The serve.auto.network parent delegation is an operator-approved
#     Namecheap change (namecheap_dns.py add-delegation), never automated.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

NAME=${1:?usage: build-site.sh <name> [--location hil] [--type t] [--public-edge]}
shift
LOCATION=hil TYPE=cpx11 PUBLIC_EDGE=0
while [ $# -gt 0 ]; do
    case $1 in
    --location) LOCATION=$2; shift 2 ;;
    --type) TYPE=$2; shift 2 ;;
    --public-edge) PUBLIC_EDGE=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

REPO_ROOT=$(cd ../../.. && pwd)

echo "== 1/4 provision $NAME ($TYPE, $LOCATION)"
IP=$(./provision-vm.sh "$NAME" --role service --type "$TYPE" --location "$LOCATION")
echo "   $NAME -> $IP"
TARGET="root@$IP"

echo "== 2/4 wait for SSH + cloud-init"
for _ in $(seq 1 60); do
    if ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 \
        "$TARGET" "cloud-init status --wait >/dev/null 2>&1 || true; \
                   command -v caddy >/dev/null && command -v python3 >/dev/null" \
        2>/dev/null; then
        echo "   reachable, cloud-init done"
        break
    fi
    sleep 5
done

echo "== 3/4 registry code deploy (carries the DNS responder)"
"$REPO_ROOT/tools/network/registry/deploy/deploy.sh" "$TARGET"

echo "== 4/4 DNS process"
./dns/deploy.sh --host "$TARGET" --node-id "$NAME"

if [ "$PUBLIC_EDGE" = 1 ]; then
    echo "== public Caddy edge (--public-edge)"
    echo "   NOTE: Let's Encrypt HTTP-01 will only succeed if DNS already"
    echo "   routes this box's hostnames to $IP. If it does not, expect"
    echo "   cert-acquisition failures — join the box to anycast/DNS first."
    ./deploy-caddy.sh "$TARGET" --bind-ip "$IP"
fi

cat <<DONE

== site $NAME is up at $IP ==
Automated span complete. Remaining GATES (by design, not omission):
  * Public TLS edge: needs DNS routing this box's hostnames to $IP first
    (anycast address, or a test hostname). Re-run with --public-edge then.
  * serve.auto.network delegation / this box as ns2: operator-approved
    Namecheap change —
      python3 namecheap_dns.py add-delegation \\
        --primary-ip <ash-ip> --secondary-ip $IP --dry-run
Verify DNS answers now:  dig +short @$IP probe.serve.auto.network A
DONE
