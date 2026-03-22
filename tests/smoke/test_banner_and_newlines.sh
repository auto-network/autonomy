#!/usr/bin/env bash
# Smoke test: SSE banner visibility + user message newline preservation
# Tests fixes from bead auto-bkbh
set -euo pipefail

PASS=0
FAIL=0
PORT=9091

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
  rm -f /tmp/test_fixtures.json /tmp/test_events.jsonl
  agent-browser close 2>/dev/null || true
}
trap cleanup EXIT

# --- Setup ---
echo "=== Setup ==="
echo '{"beads":[],"runs":[]}' > /tmp/test_fixtures.json
> /tmp/test_events.jsonl

DASHBOARD_MOCK=/tmp/test_fixtures.json \
  DASHBOARD_MOCK_EVENTS=/tmp/test_events.jsonl \
  PYTHONPATH=/workspace/repo \
  uvicorn tools.dashboard.server:app --host 0.0.0.0 --port "$PORT" &
SERVER_PID=$!
sleep 3

agent-browser open "http://localhost:$PORT/beads"
agent-browser wait --load networkidle

# --- Test 1: Banner visible on beads page ---
echo "=== Test 1: Banner visible on beads page ==="
agent-browser eval 'Alpine.store("app").sseInterrupted = "Server restarted"; "ok"'
sleep 1

RESULT=$(agent-browser eval 'var el = document.querySelector(".sse-banner"); el ? getComputedStyle(el).zIndex + " " + getComputedStyle(el).position : "missing"')
if echo "$RESULT" | grep -q "20 fixed"; then
  pass "Banner is fixed with z-index 20"
else
  fail "Banner positioning: $RESULT"
fi

RESULT=$(agent-browser eval 'document.body.classList.contains("sse-banner-visible")')
if echo "$RESULT" | grep -q "true"; then
  pass "Body has sse-banner-visible class"
else
  fail "Body class: $RESULT"
fi

# --- Test 2: Banner visible inside session viewer ---
echo "=== Test 2: Banner visible inside session viewer ==="
agent-browser eval 'navigateTo("/session/test/test-session")'
sleep 2

RESULT=$(agent-browser eval 'var el = document.querySelector(".sse-banner"); el && el.offsetHeight > 0 && el.getBoundingClientRect().top >= 0')
if echo "$RESULT" | grep -q "true"; then
  pass "Banner visible inside session viewer"
else
  fail "Banner inside session viewer: $RESULT"
fi

RESULT=$(agent-browser eval 'var sv = document.querySelector(".session-viewer"); sv ? sv.getBoundingClientRect().top : -1')
if echo "$RESULT" | grep -qE '^[3-9][0-9]|^[1-9][0-9]{2}'; then
  pass "Session viewer top shifted down for banner"
else
  fail "Session viewer top: $RESULT"
fi

# --- Test 3: Banner dismiss restores session viewer ---
echo "=== Test 3: Banner dismiss restores layout ==="
agent-browser eval 'Alpine.store("app").sseInterrupted = false; "dismissed"'
sleep 1

RESULT=$(agent-browser eval 'var sv = document.querySelector(".session-viewer"); sv ? sv.getBoundingClientRect().top : -1')
if echo "$RESULT" | grep -q "^0"; then
  pass "Session viewer returns to top:0 after dismiss"
else
  fail "Session viewer after dismiss: $RESULT"
fi

# --- Test 4: User message newlines preserved ---
echo "=== Test 4: User message newlines preserved ==="
printf '{"topic":"session:test-session","data":{"entries":[{"type":"user","role":"user","content":"Line one\\n\\nLine three\\n\\n\\n\\nLine seven","timestamp":"2026-03-22T00:00:00Z"}],"is_live":true,"seq":1}}\n' >> /tmp/test_events.jsonl
sleep 2

RESULT=$(agent-browser eval 'var el = document.querySelector(".sc-user-content"); el ? getComputedStyle(el).whiteSpace : "none"')
if echo "$RESULT" | grep -q "pre-wrap"; then
  pass "white-space: pre-wrap applied to .sc-user-content"
else
  fail "white-space: $RESULT"
fi

RESULT=$(agent-browser eval 'var el = document.querySelector(".sc-user-content"); el ? el.offsetHeight : 0')
HEIGHT=$(echo "$RESULT" | tr -d '"')
if [ "$HEIGHT" -gt 20 ] 2>/dev/null; then
  pass "User content is multi-line (height=$HEIGHT)"
else
  fail "User content height: $RESULT (expected > 20)"
fi

# --- Summary ---
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
