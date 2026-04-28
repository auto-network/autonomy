#!/usr/bin/env bash
# Smoke test: SSE banner layout + multiline user-message rendering.
set -euo pipefail

PASS=0
FAIL=0
PORT=9091
FIXTURE=/tmp/test_fixtures.json
EVENTS=/tmp/test_events.jsonl

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
      "session_id": "test-session",
      "tmux_session": "test-session",
      "project": "test",
      "type": "container",
      "is_live": true,
      "linked": true,
      "label": "Smoke session",
      "entry_count": 0,
      "context_tokens": 0,
      "last_activity": "2026-03-24T12:00:00Z",
      "latest": ""
    }
  ],
  "session_entries": {
    "test-session": []
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

agent-browser open "http://localhost:$PORT/session/test/test-session"
sleep 2

BASE_TOP=$(agent-browser eval 'var el = document.getElementById("content"); el ? el.getBoundingClientRect().top : -1')
BASE_TOP=$(echo "$BASE_TOP" | tr -d '"')

echo "=== Test 1: Banner visible on session page ==="
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

TOP_WITH_BANNER=$(agent-browser eval 'var el = document.getElementById("content"); el ? el.getBoundingClientRect().top : -1')
TOP_WITH_BANNER=$(echo "$TOP_WITH_BANNER" | tr -d '"')
if python3 - <<PY
base = float("$BASE_TOP")
top = float("$TOP_WITH_BANNER")
raise SystemExit(0 if abs(top - base) <= 8 else 1)
PY
then
  pass "Main content layout stays stable when banner overlays"
else
  fail "Main content top shifted unexpectedly: base=$BASE_TOP now=$TOP_WITH_BANNER"
fi

echo "=== Test 2: Banner dismiss restores layout ==="
agent-browser eval 'Alpine.store("app").sseInterrupted = false; "dismissed"'
sleep 1

RESULT=$(agent-browser eval 'document.body.classList.contains("sse-banner-visible")')
if echo "$RESULT" | grep -q "false"; then
  pass "Body class clears after dismiss"
else
  fail "Body class did not clear: $RESULT"
fi

TOP_AFTER=$(agent-browser eval 'var el = document.getElementById("content"); el ? el.getBoundingClientRect().top : -1')
TOP_AFTER=$(echo "$TOP_AFTER" | tr -d '"')
if python3 - <<PY
base = float("$BASE_TOP")
top = float("$TOP_AFTER")
raise SystemExit(0 if abs(top - base) <= 8 else 1)
PY
then
  pass "Main content returns to baseline after dismiss"
else
  fail "Main content did not return to baseline: base=$BASE_TOP after=$TOP_AFTER"
fi

echo "=== Test 3: User message newlines preserved ==="
printf '%s\n' '{"topic":"session:messages","data":{"session_id":"test-session","entries":[{"type":"user","content":"Line one\n\nLine three\n\n\n\nLine seven","timestamp":"2026-03-22T00:00:00Z"}]}}' >> "$EVENTS"
sleep 2

RESULT=$(agent-browser eval 'var el = document.querySelector(".sc-user-content"); el ? getComputedStyle(el).whiteSpace : "missing"')
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

RESULT=$(agent-browser eval 'var el = document.querySelector(".sc-user-content"); el ? el.textContent.indexOf("Line three") !== -1 : false')
if echo "$RESULT" | grep -q "true"; then
  pass "Rendered user content includes middle newline-separated line"
else
  fail "Rendered user content missing expected text"
fi

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
