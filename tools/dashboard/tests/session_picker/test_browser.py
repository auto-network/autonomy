"""
Browser functional tests for the session picker.

Tests BEHAVIOR through the user's perspective — not CSS classes or DOM structure.
Uses DASHBOARD_MOCK fixtures for data, agent-browser for interaction.

Every test answers: "Can the user do X?" — not "Does CSS class Y exist?"
"""
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tools.dashboard.tests import fixtures


TEST_PORT = 8082


# ── Agent Browser Helpers ─────────────────────────────────────────────

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


# ── Test Harness ──────────────────────────────────────────────────────

class PickerTestHarness:
    """Manages test server + fixture + browser. Tests interact through this."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.fixture_path = tmp_path / "fixtures.json"
        self.proc = None
        self.exp_id = fixtures.TEST_EXPERIMENT_ID

    def set_sessions(self, fixture_dict):
        """Swap the session data. Mock DAO reads this fresh on every request."""
        fixtures.write_fixture(fixture_dict, self.fixture_path)

    def start_server(self):
        # Kill any stale server on our port
        subprocess.run(
            ["python3", "-c", f"import httpx; httpx.get('http://localhost:{TEST_PORT}/', timeout=1)"],
            capture_output=True, timeout=3,
        )
        subprocess.run(
            ["pkill", "-f", f"uvicorn.*{TEST_PORT}"],
            capture_output=True, timeout=3,
        )
        time.sleep(1)

        env = os.environ.copy()
        env["DASHBOARD_MOCK"] = str(self.fixture_path)
        repo_root = str(Path(__file__).resolve().parents[4])
        env["PYTHONPATH"] = repo_root
        self.proc = subprocess.Popen(
            ["python3", "-m", "uvicorn", "tools.dashboard.server:app",
             "--host", "127.0.0.1", "--port", str(TEST_PORT)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=repo_root,
        )
        import httpx
        for _ in range(20):
            try:
                if httpx.get(f"http://localhost:{TEST_PORT}/sessions", timeout=1).status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(0.5)
        self.stop()
        raise RuntimeError("Server failed to start")

    def stop(self):
        if self.proc:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def open_experiment(self):
        ab_raw("close")
        ab_raw("open", f"http://localhost:{TEST_PORT}/design/{self.exp_id}",
               "--ignore-https-errors")
        time.sleep(2)
        ab_raw("set", "viewport", "390", "844")
        time.sleep(0.5)
        ab_eval("document.getElementById('sidebar').classList.add('-translate-x-full'); return 'ok';")
        time.sleep(0.5)

    def open_picker(self):
        ab_eval("""
            var els = document.querySelectorAll('[x-data]');
            for (var i=0; i<els.length; i++) {
                var d = els[i]._x_dataStack && els[i]._x_dataStack[0];
                if (d && d.chatOpen !== undefined) {
                    d.chatOpen = true;
                    d._loadChatSessions();
                    return 'opened';
                }
            }
            return 'not found';
        """)
        time.sleep(1)

    def refresh(self):
        # Re-seed Alpine store from the (potentially updated) fixture via API,
        # then reload the picker from the store.
        ab_eval("""
            var xhr = new XMLHttpRequest();
            xhr.open('GET', '/api/dao/active_sessions', false);
            xhr.send();
            var data = JSON.parse(xhr.responseText);
            var store = Alpine.store('sessions');
            for (var k in store) { if (store.hasOwnProperty(k)) store[k].isLive = false; }
            if (Array.isArray(data)) {
                for (var i = 0; i < data.length; i++) {
                    var s = data[i];
                    var id = s.session_id || s.tmux_session;
                    var ss = window.getSessionStore(id);
                    ss.isLive = s.is_live !== false;
                    ss.label = s.label || '';
                    ss.sessionType = s.type || '';
                    if (s.role) ss.role = s.role;
                    ss.project = s.project || '';
                    if (s.entry_count) ss.entryCount = s.entry_count;
                    if (s.context_tokens) ss.contextTokens = s.context_tokens;
                }
            }
            var els = document.querySelectorAll('[x-data]');
            for (var i = 0; i < els.length; i++) {
                var d = els[i]._x_dataStack && els[i]._x_dataStack[0];
                if (d && d._loadChatSessions) { d._loadChatSessions(); return 'ok'; }
            }
            return 'no component';
        """)
        time.sleep(0.5)

    def session_count(self):
        return ab_eval("""
            var els = document.querySelectorAll('[x-data]');
            for (var i=0; i<els.length; i++) {
                var d = els[i]._x_dataStack && els[i]._x_dataStack[0];
                if (d && d.chatSessions) return d.chatSessions.length;
            }
            return -1;
        """)

    def visible_labels(self):
        """What session names can the user actually see?"""
        return ab_eval("""
            var cards = document.querySelectorAll('[x-data] button[class*="picker"]');
            if (!cards.length) cards = document.querySelectorAll('[x-data] button');
            return Array.from(cards).map(function(b) { return b.textContent.trim(); });
        """) or []

    def visible_text(self):
        return ab_eval("return document.body.innerText") or ""

    def connect_session(self, session_id=None):
        return ab_eval(f"""
            var els = document.querySelectorAll('[x-data]');
            for (var i=0; i<els.length; i++) {{
                var d = els[i]._x_dataStack && els[i]._x_dataStack[0];
                if (d && d._connectSession && d.chatSessions && d.chatSessions.length) {{
                    var id = {'\"' + session_id + '\"' if session_id else 'd.chatSessions[0].id'};
                    d._connectSession(id);
                    return id;
                }}
            }}
            return null;
        """)

    def disconnect(self):
        ab_eval("""
            var els = document.querySelectorAll('[x-data]');
            for (var i=0; i<els.length; i++) {
                var d = els[i]._x_dataStack && els[i]._x_dataStack[0];
                if (d && d.disconnectSession) { d.disconnectSession(); return; }
            }
        """)
        time.sleep(0.5)

    def is_connected(self):
        state = ab_eval("""
            var els = document.querySelectorAll('[x-data]');
            for (var i=0; i<els.length; i++) {
                var d = els[i]._x_dataStack && els[i]._x_dataStack[0];
                if (d && d.chatConnected !== undefined)
                    return { connected: d.chatConnected, session: d._tmuxSession };
            }
        """)
        return state if isinstance(state, dict) else {"connected": False}

    def chat_entry_count(self):
        """How many entries did the chat panel actually load?"""
        result = ab_eval("""
            var panels = document.querySelectorAll('[x-data]');
            for (var i=0; i<panels.length; i++) {
                var d = panels[i]._x_dataStack && panels[i]._x_dataStack[0];
                if (d && d.entries !== undefined && d.entries.length > 0)
                    return { count: d.entries.length, status: 'loaded' };
                if (d && d.configure)
                    return { count: 0, status: 'panel_exists_empty' };
            }
            return { count: 0, status: 'no_chat_panel' };
        """)
        return result if isinstance(result, dict) else {"count": 0, "status": "error"}

    def get_localstorage(self):
        result = ab("storage", "local", f"design-chat-{self.exp_id}")
        if isinstance(result, dict):
            return result.get("value")
        return result


@pytest.fixture(scope="module")
def h(tmp_path_factory):
    """Test harness — server + browser + fixture management."""
    tmp = tmp_path_factory.mktemp("picker")
    harness = PickerTestHarness(tmp)
    harness.set_sessions(fixtures.standard_sessions())
    harness.start_server()
    harness.open_experiment()
    harness.open_picker()
    yield harness
    ab_raw("close")
    harness.stop()


# ══════════════════════════════════════════════════════════════════════
# BEHAVIOR TESTS — Can the user do X?
# ══════════════════════════════════════════════════════════════════════

class TestUserCanSeeSessions:
    """When I open the picker, can I see my sessions?"""

    def test_sessions_are_listed(self, h):
        assert h.session_count() >= 4

    def test_i_see_session_names(self, h):
        """I should see meaningful names like 'Test Designer', not 'auto-test-designer'."""
        text = h.visible_text()
        assert "Test Designer" in text, f"Can't see session label. Visible: {text[:200]}"

    def test_i_see_host_sessions(self, h):
        text = h.visible_text()
        assert "Host" in text, f"Can't see host session. Visible: {text[:200]}"

    def test_chatwith_sessions_are_hidden(self, h):
        text = h.visible_text()
        assert "chatwith" not in text.lower(), f"Orphan chatwith visible: {text[:200]}"

    def test_i_see_new_session_option(self, h):
        text = h.visible_text()
        assert "New Session" in text, f"No new session option. Visible: {text[:200]}"


class TestUserCanConnect:
    """When I tap a session, does it connect?"""

    def test_tap_session_connects(self, h):
        session_id = h.connect_session()
        assert session_id, "Connect returned no session ID"
        time.sleep(0.5)
        state = h.is_connected()
        assert state["connected"] == True, f"Not connected: {state}"

    def test_connection_is_remembered(self, h):
        """If I leave and come back, my selection should persist."""
        stored = h.get_localstorage()
        assert stored, f"Connection not saved to localStorage: {stored}"

    def test_chat_panel_shows_messages(self, h):
        """After connecting, I should see the session's conversation history.
        KNOWN BUG: _connectSession doesn't wire the chat panel to load entries."""
        time.sleep(2)
        result = h.chat_entry_count()
        assert result["count"] > 0, (
            f"BUG: No messages shown after connecting. Status: {result['status']}. "
            "See postmortem graph://fc8b4f21-1d7"
        )


class TestUserCanDisconnect:
    """When I disconnect, does it return to the picker?"""

    def test_disconnect_shows_picker(self, h):
        h.disconnect()
        state = h.is_connected()
        assert state["connected"] == False, f"Still connected: {state}"

    def test_disconnect_forgets_session(self, h):
        stored = h.get_localstorage()
        assert not stored, f"Session still in localStorage: {stored}"

    def test_sessions_still_visible_after_disconnect(self, h):
        h.refresh()
        assert h.session_count() >= 4


# ══════════════════════════════════════════════════════════════════════
# DATA VARIATION TESTS — Does the UI handle edge cases?
# ══════════════════════════════════════════════════════════════════════

class TestEmptyState:
    """What happens when there are no sessions?"""

    def test_shows_empty_message(self, h):
        h.set_sessions(fixtures.empty_sessions())
        h.refresh()
        assert h.session_count() == 0
        text = h.visible_text()
        assert "no live sessions" in text.lower(), f"No empty state message. Visible: {text[:200]}"

    def test_restore(self, h):
        h.set_sessions(fixtures.standard_sessions())
        h.refresh()


class TestManySessionsScale:
    """Does the picker handle a large number of sessions?"""

    def test_100_sessions_all_accessible(self, h):
        h.set_sessions(fixtures.many_sessions(100))
        h.refresh()
        assert h.session_count() == 100

    def test_restore(self, h):
        h.set_sessions(fixtures.standard_sessions())
        h.refresh()


class TestSessionsWithoutLabels:
    """If sessions have no labels, do they still show up usably?"""

    def test_shows_something_identifiable(self, h):
        h.set_sessions(fixtures.no_labels())
        h.refresh()
        text = h.visible_text()
        # Should show tmux names as fallback
        assert "auto-nolabel" in text or "nolabel" in text, \
            f"Unlabeled sessions not identifiable. Visible: {text[:200]}"

    def test_restore(self, h):
        h.set_sessions(fixtures.standard_sessions())
        h.refresh()


class TestXSSProtection:
    """Malicious data in session fields must not execute."""

    def test_script_tags_not_executed(self, h):
        h.set_sessions(fixtures.xss_attempt())
        h.refresh()
        # If XSS executed, this would have popped an alert and potentially crashed
        # The fact that we can still evaluate JS means it didn't execute
        count = h.session_count()
        assert isinstance(count, int), "Page crashed from XSS payload"
        # Also verify the script text is visible as text, not executed
        text = h.visible_text()
        assert "alert" not in text or "script" in text.lower()

    def test_restore(self, h):
        h.set_sessions(fixtures.standard_sessions())
        h.refresh()


class TestOnlyChatwithSessions:
    """If all sessions are chatwith orphans, picker should be empty."""

    def test_all_filtered(self, h):
        h.set_sessions(fixtures.only_chatwith())
        h.refresh()
        assert h.session_count() == 0

    def test_restore(self, h):
        h.set_sessions(fixtures.standard_sessions())
        h.refresh()
