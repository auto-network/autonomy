#!/usr/bin/env bash
# Smoke test: sessions page launch chrome + empty-state viewer for a new live session.
set -euo pipefail

PASS=0
FAIL=0
PORT=9092
FIXTURE=/tmp/test_new_session_fixture.json
EVENTS=/tmp/test_new_session_events.jsonl
LOCK=/tmp/dashboard-agent-browser-smoke.lock

# agent-browser exposes a shared daemon/session, so concurrent smoke
# scripts can close or repoint each other's page underneath the checks.
# Serialize the whole script when multiple smokes are launched together.
exec 9>"$LOCK"
flock 9

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

cleanup() {
  kill "${SERVER_PID:-}" 2>/dev/null || true
  rm -f "$FIXTURE" "$EVENTS"
  agent-browser close >/dev/null 2>&1 || true
}
trap cleanup EXIT

cat >"$FIXTURE" <<'JSON'
{
  "beads": [
    {"id": "auto-smoke", "title": "Smoke", "status": "open", "priority": 1}
  ],
  "active_sessions": [
    {
      "session_id": "auto-new-smoke",
      "tmux_session": "auto-new-smoke",
      "project": "autonomy",
      "type": "container",
      "is_live": true,
      "linked": true,
      "label": "New session smoke",
      "role": "builder",
      "entry_count": 0,
      "context_tokens": 0,
      "last_activity": "2026-03-24T12:00:00Z",
      "last_message": "Starting..."
    }
  ],
  "session_entries": {
    "auto-new-smoke": []
  }
}
JSON
>"$EVENTS"

echo "=== Setup ==="
DASHBOARD_MOCK="$FIXTURE" \
  DASHBOARD_MOCK_EVENTS="$EVENTS" \
  PYTHONPATH=/workspace/repo \
  uvicorn tools.dashboard.server:app --host 0.0.0.0 --port "$PORT" &
SERVER_PID=$!
sleep 3

echo "=== Test 1: Sessions page launch controls ==="
agent-browser open "http://localhost:$PORT/sessions"
sleep 2

RESULT=$(agent-browser eval 'document.body.textContent.indexOf("Active Sessions") !== -1')
if echo "$RESULT" | grep -q "true"; then
  pass "Sessions page loaded"
else
  fail "Sessions page did not load"
fi

PLUS_REF=$(agent-browser snapshot -i 2>&1 | grep 'button "+"' | grep -oP 'ref=\K[^]]+' | head -1)
if [ -n "$PLUS_REF" ]; then
  pass "Found launch button (ref=$PLUS_REF)"
else
  fail "Could not find launch button"
fi

if [ -n "$PLUS_REF" ]; then
  agent-browser click "$PLUS_REF" >/dev/null 2>&1
  sleep 1
  RESULT=$(agent-browser eval 'document.body.textContent.indexOf("Host Terminal") !== -1')
  if echo "$RESULT" | grep -q "true"; then
    pass "Launch dropdown shows Host Terminal option"
  else
    fail "Launch dropdown did not show Host Terminal option"
  fi
fi

RESULT=$(agent-browser eval 'document.body.textContent.indexOf("New session smoke") !== -1')
if echo "$RESULT" | grep -q "true"; then
  pass "Active session card is visible"
else
  fail "Active session card did not render"
fi

RESULT=$(agent-browser eval 'document.body.textContent.indexOf("Starting...") !== -1')
if echo "$RESULT" | grep -q "true"; then
  pass "Active session card shows startup message"
else
  fail "Active session card missing startup message"
fi

echo "=== Test 2: New live session empty state ==="
agent-browser open "http://localhost:$PORT/session/autonomy/auto-new-smoke"
sleep 2

RESULT=$(agent-browser eval 'document.body.textContent.indexOf("Session started") !== -1')
if echo "$RESULT" | grep -q "true"; then
  pass "Session viewer shows empty-state heading"
else
  fail "Session viewer missing empty-state heading"
fi

RESULT=$(agent-browser eval 'document.body.textContent.indexOf("Send a message to begin") !== -1')
if echo "$RESULT" | grep -q "true"; then
  pass "Session viewer shows empty-state body"
else
  fail "Session viewer missing empty-state body"
fi

RESULT=$(agent-browser eval 'var el = document.querySelector(".sv-input"); el ? getComputedStyle(el).display !== "none" && el.offsetHeight > 0 : false')
if echo "$RESULT" | grep -q "true"; then
  pass "Input bar is visible for live linked session"
else
  fail "Input bar not visible for live linked session"
fi

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
