"""E2E SSE gap recovery browser tests — disconnect, reconnect, verify entries.

L3 test: requires agent-browser + mock server. Tests the full SSE disconnect →
reconnect → gap recovery path through a real browser.

Infrastructure:
  - Mock server with DASHBOARD_MOCK + DASHBOARD_MOCK_EVENTS on port 8086
  - Session with initial entries loaded via backfill
  - agent-browser on the session viewer page
  - Events written to DASHBOARD_MOCK_EVENTS while browser is disconnected
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


# ── Skip if agent-browser unavailable ────────────────────────────────

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

TEST_PORT = 8086
TEST_SESSION_ID = "auto-gap-browser"
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
                "label": "Gap Recovery Browser Test",
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
    subprocess.run(
        ["pkill", "-f", f"uvicorn.*{port}"],
        capture_output=True, timeout=3,
    )
    time.sleep(0.3)

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
        time.sleep(0.2)
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

def _write_session_messages(events_path, session_id, entries):
    """Write a session:messages SSE event to the DASHBOARD_MOCK_EVENTS file."""
    line = json.dumps({"topic": "session:messages", "data": {
        "session_id": session_id,
        "entries": entries,
    }})
    with open(events_path, "a") as f:
        f.write(line + "\n")


# ── Test Harness ─────────────────────────────────────────────────────

class Harness:
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

    def stop(self):
        _stop_server(self.proc)

    def open_session_page(self):
        ab_raw("close")
        ab_raw("open", f"http://localhost:{TEST_PORT}/session/{TEST_PROJECT}/{TEST_SESSION_ID}",
               "--ignore-https-errors")
        time.sleep(2)

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

    def wait_for_backfill(self, expected_count, timeout=8):
        """Poll until session store has at least expected_count entries."""
        for _ in range(int(timeout / 0.5)):
            result = ab_eval(f"""
                var s = Alpine.store('sessions')['{TEST_SESSION_ID}'];
                if (!s) return null;
                return {{ entries: s.entries.length, loaded: s.loaded }};
            """)
            if result and result.get("entries", 0) >= expected_count:
                return result
            time.sleep(0.5)
        return result

    def prime_lastSeq(self):
        """Send a primer event so _lastSeq > 0 before disconnect.

        Gap detection requires _lastSeq > 0. On a fresh page, only cached
        events (seq=0) have been received, so we need at least one live
        broadcast.
        """
        primer = _assistant_entry("Primer event", 55)
        self.write_gap_events([primer])
        for _ in range(8):
            last_seq = ab_eval("return window._lastSeq;")
            if last_seq and last_seq > 0:
                return last_seq
            time.sleep(0.3)
        return 0


# ── Module-scoped fixture ────────────────────────────────────────────

@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gap_browser")
    h = Harness(tmp)
    h.write_fixture(_make_fixture())
    h.start_server()
    yield h
    ab_raw("close")
    h.stop()


# ═══════════════════════════════════════════════════════════════════════
# TestSmallGapRecovery — disconnect, accumulate 10 events, reconnect
# ═══════════════════════════════════════════════════════════════════════

class TestSmallGapRecovery:
    """5 tests: initial visibility, disconnect/reconnect, completeness, tool matching, no banner."""

    _initial_count = 0

    def test_initial_entries_visible(self, harness):
        """Page loads, initial entries from backfill visible."""
        harness.open_session_page()
        result = harness.wait_for_backfill(len(INITIAL_ENTRIES))
        assert result is not None, "Session store not initialized"
        assert result["entries"] >= len(INITIAL_ENTRIES), (
            f"Expected >= {len(INITIAL_ENTRIES)} entries, got {result['entries']}"
        )
        TestSmallGapRecovery._initial_count = result["entries"]

    def test_disconnect_accumulate_reconnect(self, harness):
        """Close EventSource. Write 10 events. Wait 1.5s. Reconnect. Wait 3s for replay."""
        # Disconnect
        disconnect_result = ab_eval(f"""
            window._savedSeq = window._lastSeq;
            window._savedCount = Alpine.store('sessions')['{TEST_SESSION_ID}'].entries.length;
            window._es.close();
            return {{ seq: window._savedSeq, count: window._savedCount }};
        """)
        assert disconnect_result is not None, "Failed to disconnect"

        # Write 10 gap events while disconnected
        gap_events = _make_gap_events()
        harness.write_gap_events(gap_events)

        # Wait for mock event watcher to process (polls every 0.5s)
        time.sleep(1.5)

        # Reconnect
        ab_eval("window._connect(); return 'reconnecting';")

        # Write trigger event — arrives with high seq, triggers gap detection + replay
        trigger = _assistant_entry("Trigger event for gap detection", 20)
        harness.write_gap_events([trigger])

        # Wait for gap detection + replay
        time.sleep(3)

    def test_all_entries_present_after_recovery(self, harness):
        """Entry count = initial + new. No gaps."""
        result = ab_eval(f"""
            var s = Alpine.store('sessions')['{TEST_SESSION_ID}'];
            return {{ entries: s.entries.length }};
        """)
        assert result is not None
        # 10 gap events + 1 trigger
        new_count = len(_make_gap_events()) + 1
        expected = TestSmallGapRecovery._initial_count + new_count
        assert result["entries"] >= expected, (
            f"Expected >= {expected} entries, got {result['entries']} "
            f"(initial={TestSmallGapRecovery._initial_count}, new={new_count})"
        )

    def test_tools_matched_after_recovery(self, harness):
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
        """Alpine.store('app').sseInterrupted is false."""
        result = ab_eval("return Alpine.store('app').sseInterrupted;")
        assert result is False or result is None or result == 0, (
            f"Expected sseInterrupted=false after complete replay, got {result}"
        )


# ═══════════════════════════════════════════════════════════════════════
# TestBufferOverflow — buffer can't cover the gap
# ═══════════════════════════════════════════════════════════════════════

class TestBufferOverflow:
    """2 tests: overflow shows banner, new events accepted after overflow."""

    def test_overflow_shows_banner(self, harness):
        """Close EventSource. Write enough large events to exceed 2MB buffer.
        Reconnect. Assert interruption banner visible."""
        harness.open_session_page()
        result = harness.wait_for_backfill(len(INITIAL_ENTRIES))
        assert result is not None, "Session store not initialized for overflow test"

        # Ensure _lastSeq > 0 so gap detection can trigger
        primed_seq = harness.prime_lastSeq()
        assert primed_seq > 0, "Failed to prime _lastSeq"

        # Disconnect
        ab_eval("window._es.close(); return 'disconnected';")

        # Write large events to overflow the 2MB buffer
        # Each entry ~10KB × 250 = ~2.5MB > 2MB buffer
        harness.write_large_events(count=250, size_per_entry=10000)

        # Wait for mock event watcher to process
        time.sleep(3)

        # Reconnect
        ab_eval("window._connect(); return 'reconnecting';")

        # Write trigger — arrives with high seq, buffer can't cover gap → interruption
        trigger = _assistant_entry("Overflow trigger", 59)
        harness.write_gap_events([trigger])
        time.sleep(3)

        # Check for interruption banner
        result = ab_eval("return Alpine.store('app').sseInterrupted;")
        assert result, (
            f"Expected sseInterrupted to be truthy after buffer overflow, got {result}"
        )

    def test_new_events_accepted_after_overflow(self, harness):
        """After overflow + reconnect, write one more event. Assert it appears."""
        fresh_entry = _assistant_entry("Post-overflow event", 61)
        harness.write_gap_events([fresh_entry])
        time.sleep(1.5)

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
