#!/usr/bin/env bash
# Provision a clean estate VM from a stock image. Prints the server's IPv4
# on success — the contract consumed by deployment harnesses (H2/H6) and
# service deploys (e.g. tools/network/registry/deploy/).
#
# Usage:
#   provision-vm.sh <name> [--role <role>] [--type cpx11] [--location ash]
#
# What it does (idempotent on name):
#   1. ensures the `firewall-estate` cloud firewall exists and auto-applies
#      to every estate-labeled server (in tcp 22/80/443 + icmp, any source)
#   2. creates the server: Ubuntu 24.04, project SSH key, estate labels,
#      cloud-init that installs caddy + rsync + python3-venv
#   3. waits until the server is running and cloud-init finished booting
#
# Golden-snapshot builds replace step 2's stock image later (auto-pqcsh);
# the name/IP contract stays the same.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
. ./lib.sh

NAME=${1:?usage: provision-vm.sh <name> [--role r] [--type t] [--location l]}
shift
ROLE=service TYPE=cpx11 LOCATION=ash
while [ $# -gt 0 ]; do
    case $1 in
    --role) ROLE=$2; shift 2 ;;
    --type) TYPE=$2; shift 2 ;;
    --location) LOCATION=$2; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

case "$NAME" in
"$PET_NAME"|auto-ash-1) echo "refusing: '$NAME' is the legacy pet's name" >&2; exit 1 ;;
esac

resolve_token

# Idempotency: if the name already exists, report its IP and stop.
existing=$(api GET "/servers?name=$NAME" | json_get 'd["servers"][0]["public_net"]["ipv4"]["ip"] if d["servers"] else ""')
if [ -n "$existing" ]; then
    echo "server '$NAME' already exists" >&2
    echo "$existing"
    exit 0
fi

# 1. Firewall, applied by label selector so future VMs inherit it.
fw=$(api GET "/firewalls?name=firewall-estate" | json_get 'd["firewalls"][0]["id"] if d["firewalls"] else ""')
if [ -z "$fw" ]; then
    echo "==> creating firewall-estate (applies to $ESTATE_LABEL)" >&2
    api POST /firewalls "$(python3 - "$ESTATE_LABEL" <<'PY'
import json, sys
any_src = ["0.0.0.0/0", "::/0"]
rules = [{"direction": "in", "protocol": "tcp", "port": p, "source_ips": any_src}
         for p in ("22", "80", "443")]
rules.append({"direction": "in", "protocol": "icmp", "source_ips": any_src})
print(json.dumps({
    "name": "firewall-estate",
    "rules": rules,
    "apply_to": [{"type": "label_selector",
                  "label_selector": {"selector": sys.argv[1]}}],
}))
PY
)" >/dev/null
fi

# 2. Create the server.
ssh_key=$(api GET /ssh_keys | json_get 'd["ssh_keys"][0]["name"]')
echo "==> creating $NAME ($TYPE, $LOCATION, ubuntu-24.04, key=$ssh_key)" >&2
create=$(api POST /servers "$(python3 - "$NAME" "$TYPE" "$LOCATION" "$ssh_key" "$ROLE" <<'PY'
import json, sys
name, stype, location, key, role = sys.argv[1:6]
user_data = """#cloud-config
package_update: true
packages: [caddy, rsync, python3-venv]
"""
print(json.dumps({
    "name": name, "server_type": stype, "location": location,
    "image": "ubuntu-24.04", "ssh_keys": [key],
    "labels": {"managed-by": "auto-network-estate", "role": role},
    "user_data": user_data,
}))
PY
)")
server_id=$(printf '%s' "$create" | json_get 'd["server"]["id"]')
ip=$(printf '%s' "$create" | json_get 'd["server"]["public_net"]["ipv4"]["ip"]')

# 3. Wait for running.
echo -n "==> waiting for running" >&2
for _ in $(seq 1 60); do
    status=$(api GET "/servers/$server_id" | json_get 'd["server"]["status"]')
    [ "$status" = running ] && break
    echo -n . >&2
    sleep 5
done
echo " $status" >&2
[ "$status" = running ] || { echo "server never reached running" >&2; exit 1; }

echo "==> $NAME is up (id $server_id). cloud-init installs caddy/rsync/venv on first boot (~1-2 min)." >&2
echo "$ip"
