#!/usr/bin/env bash
# Tear down an estate VM by name. Cattle only: refuses any server that does
# not carry the estate label, and hard-refuses the legacy pet (live mail)
# regardless of labels. Volumes are never touched.
#
# Usage: teardown-vm.sh <name>

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
. ./lib.sh

NAME=${1:?usage: teardown-vm.sh <name>}
resolve_token

server=$(api GET "/servers?name=$NAME")
count=$(printf '%s' "$server" | json_get 'len(d["servers"])')
if [ "$count" = 0 ]; then
    echo "no server named '$NAME'" >&2
    exit 1
fi
id=$(printf '%s' "$server" | json_get 'd["servers"][0]["id"]')
labeled=$(printf '%s' "$server" | json_get 'd["servers"][0]["labels"].get("managed-by", "")')

if [ "$id" = "$PET_ID" ] || [ "$NAME" = "$PET_NAME" ]; then
    echo "refusing: '$NAME' is the legacy pet (live mail)" >&2
    exit 1
fi
if [ "$labeled" != "auto-network-estate" ]; then
    echo "refusing: '$NAME' (id $id) is not estate-labeled ($ESTATE_LABEL)" >&2
    exit 1
fi

echo "==> deleting $NAME (id $id)" >&2
api DELETE "/servers/$id" >/dev/null
echo "deleted"
