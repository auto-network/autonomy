#!/usr/bin/env bash
# Orchestrate the row-56 capture: fire the publish request, drive the
# ceremony through the capture runner, then capture the minted link's viewer.
#
# Assumes the isolated stack is up (dashboard :8091, TLS registry :8092)
# and CAPTURE_PASSWORD / NOTE_UUID are exported. See TOOL.md.
set -euo pipefail

: "${CAPTURE_PASSWORD:?export CAPTURE_PASSWORD}"
: "${NOTE_UUID:?export NOTE_UUID}"
DASH=${DASH:-http://127.0.0.1:8091}
OUT=${OUT:-/workspace/output/evidence}
CLI_LOG=$(mktemp /tmp/row56-publish.XXXX.log)

# A fresh browser session so the capture starts at the sign-in gate.
agent-browser close --session row56cap 2>/dev/null || true

AUTONOMY_DASHBOARD=$DASH GRAPH_API=$DASH GRAPH_ORG=capture-test \
  python3 -u -m tools.graph.cli link publish "$NOTE_UUID" --type note --ttl 7d \
  --label "row56 capture" > "$CLI_LOG" 2>&1 &
CLI_PID=$!

for _ in $(seq 1 40); do
  grep -q "approval requested" "$CLI_LOG" && break
  sleep 0.5
done
APPROVAL_ID=$(grep -oE "\(([0-9a-f]+)\)" "$CLI_LOG" | tr -d '()' | head -1)
[ -n "$APPROVAL_ID" ] || { echo "no approval id; CLI log:"; cat "$CLI_LOG"; exit 1; }
export APPROVAL_ID
echo "approval: $APPROVAL_ID"

CAPTURE_DIR=$(APPROVAL_ID=$APPROVAL_ID python3 -m tools.evidence.capture \
  tools/evidence/specs/row56-publish.json --out "$OUT" --keep-open | tail -1)
echo "phase 1 captured: $CAPTURE_DIR"

wait "$CLI_PID" || { echo "publish CLI failed:"; cat "$CLI_LOG"; exit 1; }
LINK_URL=$(grep -oE "https://[^ ]+/l/[0-9a-f]+" "$CLI_LOG" | head -1)
[ -n "$LINK_URL" ] || { echo "no link URL; CLI log:"; cat "$CLI_LOG"; exit 1; }
export LINK_URL
echo "link: $LINK_URL"

python3 -m tools.evidence.capture tools/evidence/specs/row56-viewer.json \
  --continue-capture "$CAPTURE_DIR"
echo "capture complete: $CAPTURE_DIR"
