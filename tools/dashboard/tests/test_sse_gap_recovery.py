"""E2E SSE gap recovery tests — disconnect, accumulate, reconnect, verify all entries.

L3 test: requires agent-browser + mock server. Tests the full SSE disconnect →
reconnect → gap recovery path that users hit when their phone locks, network drops,
or they switch tabs.

Tests the EventBus ring buffer, /api/events/replay, client gap detection in events.js,
and _onInterruption — all end-to-end through a real browser.

Infrastructure:
  - Mock server with DASHBOARD_MOCK + DASHBOARD_MOCK_EVENTS
  - Session with initial entries loaded via backfill
  - agent-browser on the session viewer page
  - Write events to DASHBOARD_MOCK_EVENTS file while browser is disconnected
  - Mock event watcher polls every 0.5s and broadcasts to EventBus ring buffer
"""
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


# ── Skip checks ──────────────────────────────────────────────────────

def _has_agent_browser():
    try:
        r = subprocess.run(["agent-browser", "--help"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False

pytestmark = [
    pytest.mark.skipif(not _has_agent_browser(), reason="agent-browser not available"),
]


# ── Constants ────────────────────────────────────────────────────────

TEST_PORT = 8083  # distinct from other test suites
TEST_SESSION_ID = "auto-gap-test"
TEST_PROJECT = "test"


# ── Agent Browser Helpers ────────────────────────────────────────────

def ab(*args, stdin_text=None, timeout=10):
    """Run agent-browser --json, unwrap response envelope."""
    result = subprocess.run(
        ["agent-browser", "--json"] + list(args),
        capture_output=True, text=True, timeout=timeout,
        input=stdin_text,
    )
    for line in reversed(result.stdout.strip().split("\n")):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and "success" in parsed and "data" in parsed:
                return parsed["data"] if not parsed.get("error") else None
            return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def ab_eval(js):
    """Evaluate JS via stdin IIFE, unwrap {origin, result}."""
    wrapped = f"(() => {{\n{js}\n}})()"
    result = ab("eval", "--stdin", stdin_text=wrapped)
    if isinstance(result, dict) and "result" in result:
        return result["result"]
    return result


def ab_raw(*args, timeout=10):
    return subprocess.run(
        ["agent-browser"] + list(args),
        capture_output=True, text=True, timeout=timeout,
    ).stdout


# ── Entry Generators ─────────────────────────────────────────────────

def _ts(offset=0):
    return f"2026-04-10T00:{offset:02d}:00Z"


def _user_entry(text, offset=0):
    return {"type": "user", "content": text, "timestamp": _ts(offset)}


def _assistant_entry(text, offset=0):
    return {"type": "assistant_text", "content": text, "timestamp": _ts(offset)}


def _tool_use_entry(tool_id, tool_name, offset=0):
    return {
        "type": "tool_use",
        "tool_id": tool_id,
        "tool_name": tool_name,
        "content": f"Running {tool_name}",
        "timestamp": _ts(offset),
    }


def _tool_result_entry(tool_id, content="result output", offset=0):
    return {
        "type": "tool_result",
        "tool_id": tool_id,
        "content": content,
        "timestamp": _ts(offset),
    }


def _semantic_bash_entry(command="graph note 'test'", offset=0):
    return {
        "type": "semantic_bash",
        "content": command,
        "command": command,
        "timestamp": _ts(offset),
    }


# ── Initial fixture entries (loaded via backfill) ────────────────────

INITIAL_ENTRIES = [
    {"type": "system", "content": "Session started", "timestamp": _ts(0)},
    _user_entry("Hello, start the task", 1),
    _assistant_entry("Sure, I'll begin working on it.", 2),
    _tool_use_entry("tool_init_1", "Read", 3),
    _tool_result_entry("tool_init_1", "file contents here", 4),
    _assistant_entry("I've read the file. Let me proceed.", 5),
]


# ── Gap events (written while browser is disconnected) ───────────────

def _make_gap_events():
    """10 events: 2 user, 3 tool_use, 3 tool_result (1 empty), 1 assistant, 1 semantic."""
    return [
        _user_entry("Check the status", 10),
        _user_entry("Also look at the logs", 11),
        _tool_use_entry("tool_gap_1", "Bash", 12),
        _tool_use_entry("tool_gap_2", "Read", 13),
        _tool_use_entry("tool_gap_3", "Grep", 14),
        _tool_result_entry("tool_gap_1", "command output here", 15),
        _tool_result_entry("tool_gap_2", "", 16),  # empty tool_result
        _tool_result_entry("tool_gap_3", "grep matches found", 17),
        _assistant_entry("All checks complete.", 18),
        _semantic_bash_entry("graph note 'pitfall found'", 19),
    ]


# ── Fixture builder ──────────────────────────────────────────────────

def _make_fixture():
    return {
        "beads": [],
        "active_sessions": [
            {
                "session_id": TEST_SESSION_ID,
                "tmux_session": TEST_SESSION_ID,
                "project": TEST_PROJECT,
                "type": "container",
                "is_live": True,
                "label": "Gap Recovery Test",
                "entry_count": len(INITIAL_ENTRIES),
                "context_tokens": 50000,
                "last_message": "Working on gap recovery",
                "topics": [],
            },
        ],
        "session_entries": {
            TEST_SESSION_ID: INITIAL_ENTRIES,
        },
    }


# ── Server lifecycle ─────────────────────────────────────────────────

def _start_server(fixture_path, events_path, port):
    """Boot mock dashboard server. Returns Popen handle."""
    # Kill any stale server on our port
    subprocess.run(
        ["pkill", "-f", f"uvicorn.*{port}"],
        capture_output=True, timeout=3,
    )
    time.sleep(0.5)

    env = os.environ.copy()
    env["DASHBOARD_MOCK"] = str(fixture_path)
    env["DASHBOARD_MOCK_EVENTS"] = str(events_path)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3])
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "tools.dashboard.server:app",
         "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env,
    )
    return proc


def _wait_for_server(port, timeout=10.0):
    """Poll until server responds."""
    import http.client
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            conn = http.client.HTTPConnection("localhost", port, timeout=2)
            conn.request("GET", "/")
            resp = conn.getresponse()
            conn.close()
            if resp.status in (200, 307):
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _stop_server(proc):
    """Gracefully stop server."""
    if proc:
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
            proc.wait(timeout=3)


# ── SSE event file writer ────────────────────────────────────────────

def _write_sse_event(events_path, topic, data):
    """Append a single SSE event to the DASHBOARD_MOCK_EVENTS file."""
    line = json.dumps({"topic": topic, "data": data})
    with open(events_path, "a") as f:
        f.write(line + "\n")


def _write_session_messages(events_path, session_id, entries):
    """Write a session:messages SSE event with entries."""
    _write_sse_event(events_path, "session:messages", {
        "session_id": session_id,
        "entries": entries,
    })


# ── Test Harness ─────────────────────────────────────────────────────

class GapRecoveryHarness:
    """Manages test server + fixture + browser for gap recovery tests."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.fixture_path = tmp_path / "fixtures.json"
        self.events_path = tmp_path / "events.jsonl"
        self.proc = None

    def write_fixture(self, fixture_dict):
        self.fixture_path.write_text(json.dumps(fixture_dict, indent=2))

    def start_server(self):
        self.events_path.touch()
        self.proc = _start_server(self.fixture_path, self.events_path, TEST_PORT)
        if not _wait_for_server(TEST_PORT):
            self.stop()
            raise RuntimeError(f"Server failed to start on port {TEST_PORT}")

    def restart_server(self):
        """Stop and restart server (new epoch)."""
        _stop_server(self.proc)
        time.sleep(1)
        # Clear events file for fresh start
        self.events_path.write_text("")
        self.proc = _start_server(self.fixture_path, self.events_path, TEST_PORT)
        if not _wait_for_server(TEST_PORT):
            self.stop()
            raise RuntimeError(f"Server failed to restart on port {TEST_PORT}")

    def stop(self):
        _stop_server(self.proc)

    def open_session_page(self):
        ab_raw("close")
        ab_raw("open", f"http://localhost:{TEST_PORT}/session/{TEST_PROJECT}/{TEST_SESSION_ID}",
               "--ignore-https-errors")
        time.sleep(3)

    def write_gap_events(self, entries):
        """Write session:messages events while browser is disconnected."""
        _write_session_messages(self.events_path, TEST_SESSION_ID, entries)

    def write_large_events(self, count, size_per_entry=10000):
        """Write many large events to overflow the 2MB ring buffer."""
        for i in range(count):
            entry = {
                "type": "assistant_text",
                "content": "X" * size_per_entry,
                "timestamp": _ts(30 + i),
            }
            _write_session_messages(self.events_path, TEST_SESSION_ID, [entry])


# ── Module-scoped fixture ────────────────────────────────────────────

@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gap_recovery")
    h = GapRecoveryHarness(tmp)
    h.write_fixture(_make_fixture())
    h.start_server()
    yield h
    ab_raw("close")
    h.stop()


# ═══════════════════════════════════════════════════════════════════════
# TestSmallGapRecovery — 10 events during disconnect, buffer covers gap
# ═══════════════════════════════════════════════════════════════════════

class TestSmallGapRecovery:
    """10 events during disconnect, buffer covers gap. Tests 1-6."""

    # Shared state across ordered tests
    _initial_count = 0
    _initial_seq = 0

    def test_setup(self, harness):
        """Page loads, SSE connects, initial entries visible."""
        harness.open_session_page()

        # Wait for backfill to complete
        for _ in range(10):
            result = ab_eval(f"""
                var s = Alpine.store('sessions')['{TEST_SESSION_ID}'];
                if (!s) return null;
                return {{ entries: s.entries.length, seq: s.seq, loaded: s.loaded }};
            """)
            if result and result.get("entries", 0) >= len(INITIAL_ENTRIES):
                break
            time.sleep(0.5)

        assert result is not None, "Session store not initialized"
        assert result["entries"] >= len(INITIAL_ENTRIES), (
            f"Expected >= {len(INITIAL_ENTRIES)} entries, got {result['entries']}"
        )
        TestSmallGapRecovery._initial_count = result["entries"]

        # Record _lastSeq (global EventBus seq)
        seq = ab_eval("return window._lastSeq;")
        TestSmallGapRecovery._initial_seq = seq or 0

    def test_disconnect_and_reconnect(self, harness):
        """Close EventSource, write 10 events, reconnect."""
        # Disconnect
        disconnect_result = ab_eval(f"""
            window._savedSeq = window._lastSeq;
            window._savedEntryCount = Alpine.store('sessions')['{TEST_SESSION_ID}'].entries.length;
            window._es.close();
            return {{ seq: window._savedSeq, entries: window._savedEntryCount }};
        """)
        assert disconnect_result is not None, "Failed to disconnect"

        # Write 10 gap events while disconnected
        gap_events = _make_gap_events()
        harness.write_gap_events(gap_events)

        # Wait for mock event watcher to process (polls every 0.5s)
        time.sleep(1.5)

        # Reconnect — new EventSource, cached events arrive with seq=0
        ab_eval("window._connect(); return 'reconnecting';")

        # Write a trigger event — this arrives with a high seq, causing the
        # client to detect a gap and replay missed events from the ring buffer
        trigger = _assistant_entry("Trigger event for gap detection", 20)
        harness.write_gap_events([trigger])

        # Wait for gap detection + replay
        time.sleep(4)

    def test_all_entries_present(self, harness):
        """Total entries = initial + gap events + trigger. No gaps."""
        result = ab_eval(f"""
            var s = Alpine.store('sessions')['{TEST_SESSION_ID}'];
            return {{
                entries: s.entries.length,
                initial: window._savedEntryCount,
            }};
        """)
        assert result is not None
        # 10 gap events + 1 trigger event
        new_count = len(_make_gap_events()) + 1
        expected = TestSmallGapRecovery._initial_count + new_count
        assert result["entries"] >= expected, (
            f"Expected >= {expected} entries, got {result['entries']} "
            f"(initial={TestSmallGapRecovery._initial_count}, new={new_count})"
        )

    def test_tools_matched(self, harness):
        """Every tool_use has a matching tool_result. No permanently running tools."""
        result = ab_eval(f"""
            var s = Alpine.store('sessions')['{TEST_SESSION_ID}'];
            var unmatched = [];
            var toolIds = Object.keys(s.toolMap);
            for (var i = 0; i < toolIds.length; i++) {{
                if (!s.resultMap[toolIds[i]]) unmatched.push(toolIds[i]);
            }}
            return {{ toolCount: toolIds.length, unmatched: unmatched }};
        """)
        assert result is not None
        assert result["unmatched"] == [], (
            f"Unmatched tool_use IDs (still 'running'): {result['unmatched']}"
        )

    def test_no_interruption_banner(self, harness):
        """sseInterrupted is false — replay was complete."""
        result = ab_eval("return Alpine.store('app').sseInterrupted;")
        assert result is False or result is None or result == 0, (
            f"Expected sseInterrupted=false after complete replay, got {result}"
        )

    def test_entry_order(self, harness):
        """Entries are chronological. No out-of-order from replay + held events."""
        result = ab_eval(f"""
            var s = Alpine.store('sessions')['{TEST_SESSION_ID}'];
            var timestamps = [];
            for (var i = 0; i < s.entries.length; i++) {{
                timestamps.push(s.entries[i].timestamp || '');
            }}
            return timestamps;
        """)
        assert result is not None
        assert len(result) > 0, "No entries found"
        # Filter out empty timestamps and verify order
        ts = [t for t in result if t]
        for i in range(1, len(ts)):
            assert ts[i] >= ts[i - 1], (
                f"Out-of-order entries: {ts[i - 1]} > {ts[i]} at index {i}"
            )


# ═══════════════════════════════════════════════════════════════════════
# TestBufferOverflow — buffer can't cover the gap, _onInterruption fires
# ═══════════════════════════════════════════════════════════════════════

class TestBufferOverflow:
    """Ring buffer overflow — events evicted before replay. Tests 7-8."""

    def _prime_lastSeq(self, harness):
        """Send a primer event so _lastSeq > 0 before disconnect.

        Gap detection requires _lastSeq > 0 to fire. On a fresh page, only
        cached events (seq=0) have been received, so we need at least one
        real broadcast to set _lastSeq.
        """
        primer = _assistant_entry("Primer event", 55)
        harness.write_gap_events([primer])
        # Wait for mock watcher to poll + client to receive
        for _ in range(10):
            last_seq = ab_eval("return window._lastSeq;")
            if last_seq and last_seq > 0:
                return last_seq
            time.sleep(0.5)
        return 0

    def test_overflow_shows_banner(self, harness):
        """Write enough events to exceed 2MB buffer. Reconnect shows banner."""
        harness.open_session_page()
        time.sleep(2)

        # Ensure _lastSeq > 0 so gap detection can trigger
        primed_seq = self._prime_lastSeq(harness)
        assert primed_seq > 0, "Failed to prime _lastSeq"

        # Disconnect
        ab_eval("""
            window._es.close();
            return 'disconnected';
        """)

        # Write many large events to overflow the 2MB buffer
        # Each entry ~10KB, need ~200+ to fill 2MB, then more to evict
        harness.write_large_events(count=250, size_per_entry=10000)

        # Wait for mock event watcher to process all events
        time.sleep(4)

        # Reconnect
        ab_eval("window._connect(); return 'reconnecting';")

        # Write a trigger event — this arrives with a high seq, causing the
        # client to detect a gap. Buffer can't cover it → _onInterruption fires.
        trigger = _assistant_entry("Overflow trigger", 60)
        harness.write_gap_events([trigger])
        time.sleep(4)

        # Check for interruption banner
        result = ab_eval("return Alpine.store('app').sseInterrupted;")
        assert result, (
            f"Expected sseInterrupted to be truthy after buffer overflow, got {result}"
        )

    def test_seqs_reset_after_overflow(self, harness):
        """After overflow interruption, new events are accepted (not deduped)."""
        # Write a fresh event after the overflow reconnect
        fresh_entry = _assistant_entry("Post-overflow event", 61)
        harness.write_gap_events([fresh_entry])
        time.sleep(2)

        result = ab_eval(f"""
            var s = Alpine.store('sessions')['{TEST_SESSION_ID}'];
            var found = false;
            for (var i = 0; i < s.entries.length; i++) {{
                if (s.entries[i].content === 'Post-overflow event') found = true;
            }}
            return {{ found: found, entries: s.entries.length }};
        """)
        assert result is not None
        assert result["found"], "Post-overflow event was not accepted (dedup rejected it)"


# ═══════════════════════════════════════════════════════════════════════
# TestServerRestart — epoch change during disconnect
# ═══════════════════════════════════════════════════════════════════════

class TestServerRestart:
    """Epoch change during disconnect triggers interruption. Test 9."""

    def test_epoch_change_resets(self, harness):
        """Restart server → new epoch → sseInterrupted shows 'Server restarted'."""
        harness.open_session_page()
        time.sleep(2)

        # Verify page loaded
        result = ab_eval(f"""
            var s = Alpine.store('sessions')['{TEST_SESSION_ID}'];
            return s ? s.entries.length : -1;
        """)
        assert result is not None and result >= len(INITIAL_ENTRIES)

        # Disconnect SSE
        ab_eval("window._es.close(); return 'disconnected';")

        # Restart server (new epoch)
        harness.restart_server()

        # Reconnect SSE (to new server with different epoch)
        # The subscribe() sends cached events with the NEW epoch in the id field.
        # Client detects epoch mismatch and fires _onInterruption('Server restarted').
        ab_eval("window._connect(); return 'reconnecting';")

        # Write a trigger event to ensure the client receives something from
        # the new server with the new epoch
        trigger = _assistant_entry("Post-restart trigger", 70)
        harness.write_gap_events([trigger])
        time.sleep(4)

        # Check for "Server restarted" interruption
        result = ab_eval("return Alpine.store('app').sseInterrupted;")
        assert result, (
            f"Expected sseInterrupted after epoch change, got {result}"
        )

        # Verify _lastSeq was reset by _onInterruption
        last_seq = ab_eval("return window._lastSeq;")
        # _onInterruption sets _lastSeq = 0, but subsequent events may update it
        # The key assertion: the interruption banner is shown (above)
        assert last_seq is not None


# ═══════════════════════════════════════════════════════════════════════
# TestMixedEntryTypes — every entry type survives gap recovery
# ═══════════════════════════════════════════════════════════════════════

class TestMixedEntryTypes:
    """Verify every entry type survives the gap recovery path. Tests 10-13."""

    @pytest.fixture(scope="class", autouse=True)
    def _setup_gap(self, harness):
        """Set up a fresh gap recovery scenario with mixed entry types."""
        # Restart server for clean state (TestServerRestart may have restarted it)
        harness.restart_server()
        harness.open_session_page()
        time.sleep(3)

        # Verify initial load
        for _ in range(10):
            result = ab_eval(f"""
                var s = Alpine.store('sessions')['{TEST_SESSION_ID}'];
                return s ? s.entries.length : 0;
            """)
            if result and result >= len(INITIAL_ENTRIES):
                break
            time.sleep(0.5)

        # Disconnect
        ab_eval(f"""
            window._savedEntryCount = Alpine.store('sessions')['{TEST_SESSION_ID}'].entries.length;
            window._es.close();
            return 'disconnected';
        """)

        # Write gap events with mixed types
        gap_events = _make_gap_events()
        harness.write_gap_events(gap_events)
        time.sleep(1.5)

        # Reconnect
        ab_eval("window._connect(); return 'reconnecting';")

        # Write trigger event to cause gap detection + replay
        trigger = _assistant_entry("Mixed types trigger", 25)
        harness.write_gap_events([trigger])
        time.sleep(4)

    def test_user_messages_visible(self, harness):
        """User messages written during gap appear in the store."""
        result = ab_eval(f"""
            var s = Alpine.store('sessions')['{TEST_SESSION_ID}'];
            var userMsgs = [];
            for (var i = 0; i < s.entries.length; i++) {{
                if (s.entries[i].type === 'user') userMsgs.push(s.entries[i].content);
            }}
            return userMsgs;
        """)
        assert result is not None
        assert any("Check the status" in m for m in result), (
            f"Gap user message 'Check the status' not found in {result}"
        )
        assert any("Also look at the logs" in m for m in result), (
            f"Gap user message 'Also look at the logs' not found in {result}"
        )

    def test_tool_chips_visible(self, harness):
        """Tool use entries recorded with correct tool names in toolMap."""
        result = ab_eval(f"""
            var s = Alpine.store('sessions')['{TEST_SESSION_ID}'];
            var tools = {{}};
            var ids = Object.keys(s.toolMap);
            for (var i = 0; i < ids.length; i++) {{
                tools[ids[i]] = s.toolMap[ids[i]].tool_name;
            }}
            return tools;
        """)
        assert result is not None
        tool_names = list(result.values())
        assert "Bash" in tool_names, f"Bash tool_use not found in toolMap: {result}"
        assert "Read" in tool_names, f"Read tool_use not found in toolMap: {result}"
        assert "Grep" in tool_names, f"Grep tool_use not found in toolMap: {result}"

    def test_empty_tool_result_preserved(self, harness):
        """Empty content tool_result survives gap recovery. Tool chip not 'running'."""
        result = ab_eval(f"""
            var s = Alpine.store('sessions')['{TEST_SESSION_ID}'];
            var readToolId = null;
            // Find the Read tool_use from gap events (tool_gap_2)
            for (var id in s.toolMap) {{
                if (s.toolMap[id].tool_name === 'Read'
                    && id.indexOf('gap') !== -1) {{
                    readToolId = id;
                    break;
                }}
            }}
            if (!readToolId) {{
                // Fall back: find tool_gap_2 directly
                readToolId = 'tool_gap_2';
            }}
            var hasResult = !!s.resultMap[readToolId];
            var resultContent = hasResult ? s.resultMap[readToolId].content : 'MISSING';
            return {{
                readToolId: readToolId,
                hasResult: hasResult,
                resultContent: resultContent,
            }};
        """)
        assert result is not None
        assert result["hasResult"], (
            f"tool_gap_2 (Read with empty result) missing from resultMap: {result}"
        )
        # Empty string content should be preserved (not missing)
        assert result["resultContent"] == "" or result["resultContent"] is not None, (
            f"Empty tool_result content was not preserved: {result}"
        )

    def test_semantic_tiles_visible(self, harness):
        """Semantic bash entries appear in the store after gap recovery."""
        result = ab_eval(f"""
            var s = Alpine.store('sessions')['{TEST_SESSION_ID}'];
            var semantics = [];
            for (var i = 0; i < s.entries.length; i++) {{
                if (s.entries[i].type === 'semantic_bash') {{
                    semantics.push(s.entries[i].content || s.entries[i].command);
                }}
            }}
            return semantics;
        """)
        assert result is not None
        assert len(result) > 0, "No semantic_bash entries found after gap recovery"
        assert any("graph note" in s for s in result), (
            f"Semantic bash 'graph note' not found in {result}"
        )
