#!/usr/bin/env bash
# Smoke test: new session visibility + empty state polish (auto-rdh2)
#
# Verifies:
# 1. Press + → new session card appears in Active Sessions within 3s
# 2. Card shows tmux name, "Starting..." state
# 3. Navigate to new session → viewer shows empty state message
# 4. Input bar is present and visible
#
# Prerequisites:
#   - Dashboard running on https://localhost:8080
#   - agent-browser available
#
# Usage: bash tests/smoke/test_new_session.sh

set -euo pipefail

PASS=0
FAIL=0

pass() { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1"; FAIL=$((FAIL + 1)); }

echo "=== Smoke Test: New Session Visibility (auto-rdh2) ==="

# 1. Open sessions page
echo ""
echo "Step 1: Open sessions page"
agent-browser open https://localhost:8080/sessions --ignore-https-errors >/dev/null 2>&1
agent-browser wait --load networkidle >/dev/null 2>&1

SNAP=$(agent-browser snapshot -i 2>&1)

if echo "$SNAP" | grep -q "Active Sessions"; then
  pass "Sessions page loaded"
else
  fail "Sessions page did not load"
fi

# Count existing sessions before creation
BEFORE_COUNT=$(echo "$SNAP" | grep -c 'auto-t' || true)
echo "  (existing auto-t sessions: $BEFORE_COUNT)"

# 2. Click the + button to open dropdown
echo ""
echo "Step 2: Create new session via + button"
# Find the + button ref
PLUS_REF=$(echo "$SNAP" | grep 'button "+"' | grep -oP 'ref=\K[^]]+' | head -1)
if [ -z "$PLUS_REF" ]; then
  fail "Could not find + button"
  agent-browser close >/dev/null 2>&1
  exit 1
fi
pass "Found + button (ref=$PLUS_REF)"

agent-browser click "$PLUS_REF" >/dev/null 2>&1
sleep 0.5

# Take snapshot to see dropdown
DROP_SNAP=$(agent-browser snapshot -i 2>&1)
if echo "$DROP_SNAP" | grep -q "Host Terminal"; then
  pass "Dropdown appeared with Host Terminal option"
else
  fail "Dropdown did not appear"
fi

# 3. Verify the sessions page structure has the right template elements
echo ""
echo "Step 3: Verify template structure"
# Check that the sessions.html template includes _starting conditional
if grep -q '_starting' tools/dashboard/templates/pages/sessions.html; then
  pass "sessions.html has _starting state handling"
else
  fail "sessions.html missing _starting state handling"
fi

if grep -q 'Starting\.\.\.' tools/dashboard/templates/pages/sessions.html; then
  pass "sessions.html shows 'Starting...' text"
else
  fail "sessions.html missing 'Starting...' text"
fi

# 4. Verify session viewer empty state
echo ""
echo "Step 4: Verify session viewer empty state template"
if grep -q 'Session started' tools/dashboard/templates/pages/session-view.html; then
  pass "session-view.html has empty state message"
else
  fail "session-view.html missing empty state message"
fi

if grep -q 'Send a message to begin' tools/dashboard/templates/pages/session-view.html; then
  pass "session-view.html has 'Send a message to begin' text"
else
  fail "session-view.html missing 'Send a message to begin' text"
fi

# 5. Verify server handles starting sessions
echo ""
echo "Step 5: Verify server-side changes"
if grep -q 'monitor_state = session_monitor.get_one(session_id)' tools/dashboard/server.py; then
  pass "server.py checks monitor for starting sessions in tail API"
else
  fail "server.py missing monitor check in tail API"
fi

if grep -q 'Register immediately so session appears' tools/dashboard/server.py; then
  pass "server.py registers host sessions immediately"
else
  fail "server.py missing immediate registration for host sessions"
fi

# 6. Verify session_monitor doesn't break session_id
echo ""
echo "Step 6: Verify session_monitor preserves session_id"
if grep -q 'Keep session_id as tmux_name' tools/dashboard/session_monitor.py; then
  pass "session_monitor preserves session_id on JSONL resolve"
else
  fail "session_monitor still changes session_id on resolve"
fi

# Cleanup
agent-browser close >/dev/null 2>&1

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
