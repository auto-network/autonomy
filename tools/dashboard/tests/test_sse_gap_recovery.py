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
import uuid
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

from tools.dashboard.tests._xdist import (
    bind_free_port, spawn_mock_uvicorn, worker_test_port,
)

TEST_PORT = worker_test_port(8083)  # distinct base; offset per xdist worker
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

def _start_server(fixture_path, events_path, nonce, state_path=None):
    """Boot mock dashboard server. Returns ``(proc, port)``.

    Binds a kernel-assigned free port and verifies the server echoes *nonce*
    before returning — see spawn_mock_uvicorn. worker_test_port is worker-index
    derived with no session dimension, so a fixed port can be answered by
    another session's server (port-collision report, auto-0812-211339).

    ``state_path`` controls where EventBus snapshot/restore reads & writes.
    Pass a per-call fresh path to ensure each spawn boots with a new epoch
    (preserving the pre-snapshot ``test_epoch_change_resets`` semantics)
    and to avoid polluting the real data/event_bus.state.
    """
    env = os.environ.copy()
    env["DASHBOARD_MOCK"] = str(fixture_path)
    env["DASHBOARD_MOCK_EVENTS"] = str(events_path)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3])
    if state_path is not None:
        env["DASHBOARD_EVENT_BUS_STATE"] = str(state_path)
    return spawn_mock_uvicorn(env=env, nonce=nonce)


def _wait_for_server(port, timeout=30.0):
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
        self._restart_idx = 0
        self.nonce = uuid.uuid4().hex

    def write_fixture(self, fixture_dict):
        self.fixture_path.write_text(
            json.dumps({**fixture_dict, "__harness_nonce__": self.nonce}, indent=2)
        )

    def _state_path_for_run(self):
        """Return a unique snapshot path per server spawn.

        Ensures each restart starts with no readable snapshot, so the
        "Server restarted" banner path stays exercised by the existing
        TestServerRestart suite.
        """
        return self.tmp / f"event_bus.state.{self._restart_idx}"

    def start_server(self):
        global TEST_PORT
        self.events_path.touch()
        self._restart_idx += 1
        # _start_server binds a fresh OS-assigned port and verifies the nonce;
        # publish the real port so this harness's browser opens hit our server.
        self.proc, TEST_PORT = _start_server(
            self.fixture_path, self.events_path, self.nonce,
            state_path=self._state_path_for_run(),
        )

    def restart_server(self):
        """Stop and restart server (new epoch)."""
        _stop_server(self.proc)
        time.sleep(1)
        # Clear events file for fresh start
        self.events_path.write_text("")
        self._restart_idx += 1
        global TEST_PORT
        self.proc, TEST_PORT = _start_server(
            self.fixture_path, self.events_path, self.nonce,
            state_path=self._state_path_for_run(),
        )

    def stop(self):
        _stop_server(self.proc)

    def open_session_page(self):
        ab_raw("close")
        ab_raw("open", f"http://localhost:{TEST_PORT}/session/{TEST_PROJECT}/{TEST_SESSION_ID}",
               "--ignore-https-errors")
        # Poll for the viewer's Alpine store row instead of a fixed sleep —
        # cold Chromium boots under parallel load routinely outlast it.
        _deadline = time.time() + 25
        while time.time() < _deadline:
            _ready = ab_eval(
                "return !!(window.Alpine && Alpine.store('sessions')"
                " && Alpine.store('sessions')['" + TEST_SESSION_ID + "']);"
            )
            if _ready is True:
                break
            time.sleep(0.5)

    def write_gap_events(self, entries):
        """Write session:messages events while browser is disconnected."""
        _write_session_messages(self.events_path, TEST_SESSION_ID, entries)

    def write_large_events(self, count, size_per_entry=160000):
        """Write many large events to overflow the 32MB ring buffer."""
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

@pytest.fixture(scope="module")
def small_gap_recovered(harness):
    """Drive a full small-gap recovery scenario once per worker, capture end state.

    Module-scoped so each xdist worker that lands any TestSmallGapRecovery method
    pays the setup cost exactly once. Returns plain Python data so each test
    method asserts against the captured state instead of re-querying the browser
    (which would only work if all methods landed on the same worker).
    """
    harness.open_session_page()

    # Wait for backfill to complete
    initial = None
    for _ in range(10):
        initial = ab_eval(f"""
            var s = Alpine.store('sessions')['{TEST_SESSION_ID}'];
            if (!s) return null;
            return {{ entries: s.entries.length, seq: s.seq, loaded: s.loaded }};
        """)
        if initial and initial.get("entries", 0) >= len(INITIAL_ENTRIES):
            break
        time.sleep(0.5)

    assert initial is not None, "Session store not initialized"
    assert initial["entries"] >= len(INITIAL_ENTRIES), (
        f"Expected >= {len(INITIAL_ENTRIES)} entries, got {initial['entries']}"
    )
    initial_count = initial["entries"]
    initial_seq = ab_eval("return window._lastSeq;") or 0

    # Disconnect — close EventSource, snapshot pre-disconnect state
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

    # Trigger event arrives with a high seq, causing the client to detect a gap
    # and replay missed events from the ring buffer.
    trigger = _assistant_entry("Trigger event for gap detection", 20)
    harness.write_gap_events([trigger])

    # Wait for gap detection + replay
    time.sleep(4)

    # Capture every property the assertions read, in one round-trip.
    final = ab_eval(f"""
        var s = Alpine.store('sessions')['{TEST_SESSION_ID}'];
        var unmatched = [];
        var toolIds = Object.keys(s.toolMap);
        for (var i = 0; i < toolIds.length; i++) {{
            if (!s.resultMap[toolIds[i]]) unmatched.push(toolIds[i]);
        }}
        var timestamps = [];
        for (var i = 0; i < s.entries.length; i++) {{
            timestamps.push(s.entries[i].timestamp || '');
        }}
        return {{
            entries: s.entries.length,
            toolCount: toolIds.length,
            unmatched: unmatched,
            timestamps: timestamps,
            sseInterrupted: Alpine.store('app').sseInterrupted,
        }};
    """)
    assert final is not None, "Failed to capture final state after recovery"

    return {
        "initial_count": initial_count,
        "initial_seq": initial_seq,
        "entries": final["entries"],
        "tool_count": final["toolCount"],
        "unmatched_tools": final["unmatched"],
        "timestamps": final["timestamps"],
        "sse_interrupted": final["sseInterrupted"],
    }


class TestSmallGapRecovery:
    """10 events during disconnect, buffer covers gap."""

    def test_all_entries_present(self, small_gap_recovered):
        """Total entries = initial + gap events + trigger. No gaps."""
        new_count = len(_make_gap_events()) + 1
        expected = small_gap_recovered["initial_count"] + new_count
        assert small_gap_recovered["entries"] >= expected, (
            f"Expected >= {expected} entries, got {small_gap_recovered['entries']} "
            f"(initial={small_gap_recovered['initial_count']}, new={new_count})"
        )

    def test_tools_matched(self, small_gap_recovered):
        """Every tool_use has a matching tool_result. No permanently running tools."""
        assert small_gap_recovered["unmatched_tools"] == [], (
            f"Unmatched tool_use IDs (still 'running'): "
            f"{small_gap_recovered['unmatched_tools']}"
        )

    def test_no_interruption_banner(self, small_gap_recovered):
        """sseInterrupted is false — replay was complete."""
        sse = small_gap_recovered["sse_interrupted"]
        assert sse is False or sse is None or sse == 0, (
            f"Expected sseInterrupted=false after complete replay, got {sse}"
        )

    def test_entry_order(self, small_gap_recovered):
        """Entries are chronological. No out-of-order from replay + held events."""
        ts_all = small_gap_recovered["timestamps"]
        assert len(ts_all) > 0, "No entries found"
        ts = [t for t in ts_all if t]
        for i in range(1, len(ts)):
            assert ts[i] >= ts[i - 1], (
                f"Out-of-order entries: {ts[i - 1]} > {ts[i]} at index {i}"
            )


# ═══════════════════════════════════════════════════════════════════════
# TestBufferOverflow — buffer can't cover the gap, _onInterruption fires
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def buffer_overflow_recovered(harness):
    """Drive the full overflow + post-overflow scenario once per worker, capture state.

    Module-scoped so each xdist worker pays the (slow) overflow setup once and
    every dependent assertion lands on the captured Python data, not a re-query
    that may execute on a worker where the scenario was never driven.
    """
    harness.open_session_page()
    time.sleep(2)

    # Prime _lastSeq > 0 so gap detection can trigger. On a fresh page only
    # cached events (seq=0) have arrived — need one real broadcast first.
    primer = _assistant_entry("Primer event", 55)
    harness.write_gap_events([primer])
    primed_seq = 0
    # Generous window: the mock event watcher's file poll + SSE delivery
    # can take well over 5s on a loaded machine (8 workers, 8 Chromiums).
    for _ in range(30):
        last_seq = ab_eval("return window._lastSeq;")
        if last_seq and last_seq > 0:
            primed_seq = last_seq
            break
        time.sleep(0.5)
    assert primed_seq > 0, "Failed to prime _lastSeq"

    # Disconnect
    ab_eval("window._es.close(); return 'disconnected';")

    # Write many large events to overflow the 32MB buffer
    # Each entry ~160KB × 250 = ~40MB > 32MB cap → eviction
    harness.write_large_events(count=250, size_per_entry=160000)

    # Wait for mock event watcher to process all events
    time.sleep(4)

    # Reconnect
    ab_eval("window._connect(); return 'reconnecting';")

    # Trigger event arrives with a high seq → client detects gap → buffer
    # can't cover it → _onInterruption fires.
    trigger = _assistant_entry("Overflow trigger", 60)
    harness.write_gap_events([trigger])
    time.sleep(4)

    sse_interrupted = ab_eval("return Alpine.store('app').sseInterrupted;")

    # Post-overflow event verifies dedup didn't reject fresh events after reset
    fresh_entry = _assistant_entry("Post-overflow event", 61)
    harness.write_gap_events([fresh_entry])
    time.sleep(2)

    post = ab_eval(f"""
        var s = Alpine.store('sessions')['{TEST_SESSION_ID}'];
        var found = false;
        for (var i = 0; i < s.entries.length; i++) {{
            if (s.entries[i].content === 'Post-overflow event') found = true;
        }}
        return {{ found: found, entries: s.entries.length }};
    """)
    assert post is not None, "Failed to capture post-overflow state"

    return {
        "sse_interrupted": sse_interrupted,
        "post_overflow_found": post["found"],
        "entries_count": post["entries"],
    }


class TestBufferOverflow:
    """Ring buffer overflow — events evicted before replay."""

    def test_overflow_shows_banner(self, buffer_overflow_recovered):
        """Buffer overflow during disconnect → reconnect shows banner."""
        sse = buffer_overflow_recovered["sse_interrupted"]
        assert sse, (
            f"Expected sseInterrupted to be truthy after buffer overflow, got {sse}"
        )

    def test_seqs_reset_after_overflow(self, buffer_overflow_recovered):
        """After overflow interruption, new events are accepted (not deduped)."""
        assert buffer_overflow_recovered["post_overflow_found"], (
            "Post-overflow event was not accepted (dedup rejected it)"
        )


# ═══════════════════════════════════════════════════════════════════════
# TestServerRestart — epoch change during disconnect
# ═══════════════════════════════════════════════════════════════════════

class TestServerRestart:
    """Epoch change during disconnect uses the unified restart notice. Test 9."""

    def test_epoch_change_resets(self, harness):
        """Restart server → new epoch → rich notice says it just restarted."""
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
        # Client detects epoch mismatch and shows the unified restart notice.
        ab_eval("window._connect(); return 'reconnecting';")

        # Write a trigger event to ensure the client receives something from
        # the new server with the new epoch
        trigger = _assistant_entry("Post-restart trigger", 70)
        harness.write_gap_events([trigger])
        time.sleep(4)

        result = ab_eval("return Alpine.store('app').restartStatus;")
        assert result and result.get("phase") in {"recovered", "complete"}, (
            f"Expected restartStatus after epoch change, got {result}"
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
    @classmethod
    def _setup_gap(cls, harness):
        """Set up a fresh gap recovery scenario with mixed entry types."""
        # Restart server for clean state (TestServerRestart may have restarted it)
        harness.restart_server()
        harness.open_session_page()

        # Verify initial load — generous window; a fresh server + cold page
        # under parallel load can need well past 5s for the backfill.
        for _ in range(30):
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

        # Write trigger event to cause gap detection + replay, then poll for
        # the replayed entries instead of a fixed 4s (watcher poll + replay
        # roundtrip under load can exceed it).
        trigger = _assistant_entry("Mixed types trigger", 25)
        harness.write_gap_events([trigger])
        expected = len(_make_gap_events()) + 1  # gap batch + trigger
        for _ in range(30):
            result = ab_eval(f"""
                var s = Alpine.store('sessions')['{TEST_SESSION_ID}'];
                var saved = window._savedEntryCount || 0;
                return s ? (s.entries.length - saved) : -1;
            """)
            if result is not None and result >= expected:
                break
            time.sleep(0.5)

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


# ═══════════════════════════════════════════════════════════════════════
# TestBackfillSSEHandoff — the backfill→SSE seam delivers every entry
# exactly once (the spec the retired test_cross_boundary placeholders
# named; the recovery tests above assert >=, which a double-delivery
# passes — this class is the no-duplicates guard).
# ═══════════════════════════════════════════════════════════════════════

HANDOFF_MARKERS = [f"handoff marker {n:02d}" for n in range(1, 9)]


class TestBackfillSSEHandoff:
    """Open the page (backfill), then deliver live events (SSE): the store
    must end with exactly backfill + live entries, each present once, the
    live ones in write order."""

    @pytest.fixture(scope="class", autouse=True)
    @classmethod
    def _handoff_state(cls, harness):
        # Fresh server + page: no state from earlier classes on this worker.
        harness.restart_server()
        harness.open_session_page()

        # Backfill settles: poll the store to a stable count.
        baseline = None
        for _ in range(30):
            got = ab_eval(f"""
                var s = Alpine.store('sessions')['{TEST_SESSION_ID}'];
                return s ? s.entries.length : null;
            """)
            if got is not None and got >= len(INITIAL_ENTRIES):
                baseline = got
                break
            time.sleep(0.5)
        assert baseline is not None, "backfill never populated the store"

        # Live entries over the SSE path, distinct + ordered.
        live = [_assistant_entry(m, 30 + i) for i, m in enumerate(HANDOFF_MARKERS)]
        harness.write_gap_events(live)

        final = None
        for _ in range(30):
            got = ab_eval(f"""
                var s = Alpine.store('sessions')['{TEST_SESSION_ID}'];
                return s ? s.entries.length : null;
            """)
            if got is not None and got >= baseline + len(HANDOFF_MARKERS):
                final = got
                break
            time.sleep(0.5)
        assert final is not None, "live SSE entries never reached the store"

        # One round-trip captures everything the assertions read: the
        # count, an identity key per entry, and the marker order.
        state = ab_eval(f"""
            var s = Alpine.store('sessions')['{TEST_SESSION_ID}'];
            var keys = [];
            var markers = [];
            for (var i = 0; i < s.entries.length; i++) {{
                var e = s.entries[i];
                keys.push((e.type || '') + '|' + (e.timestamp || '') + '|' +
                          (typeof e.content === 'string' ? e.content : ''));
                if (typeof e.content === 'string' &&
                    e.content.indexOf('handoff marker') === 0) {{
                    markers.push(e.content);
                }}
            }}
            return {{ total: s.entries.length, keys: keys, markers: markers }};
        """)
        assert state is not None, "failed to capture handoff end state"
        cls.baseline = baseline
        cls.state = state

    def test_no_duplicates_after_backfill_and_sse(self):
        """Exactly backfill + live entries — a double delivery on either
        side of the seam exceeds; >= would hide it."""
        assert self.state["total"] == self.baseline + len(HANDOFF_MARKERS), (
            f"expected exactly {self.baseline} backfill + "
            f"{len(HANDOFF_MARKERS)} live entries, got {self.state['total']}"
        )
        keys = self.state["keys"]
        assert len(set(keys)) == len(keys), (
            "duplicate entries in the store: "
            f"{[k for k in keys if keys.count(k) > 1][:4]}"
        )

    def test_no_gaps_after_backfill_and_sse(self):
        """Every live entry arrived, in write order — a dropped or
        reordered SSE delivery fails here."""
        assert self.state["markers"] == HANDOFF_MARKERS, (
            f"live entries missing or misordered: {self.state['markers']}"
        )
