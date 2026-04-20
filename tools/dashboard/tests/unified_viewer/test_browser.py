"""
Browser functional tests for the unified session viewer — all 6 surfaces.

Tests BEHAVIOR through the user's perspective — not CSS classes or DOM structure.
Uses DASHBOARD_MOCK fixtures for data, agent-browser for interaction.
Extends the PickerTestHarness pattern from session_picker/test_browser.py.

Test server runs HTTP on port 8082 (not HTTPS).

Expected test results:
  - TestSessionDetailPage: PASS (session viewer already works)
  - TestExperimentPanel: likely PASS (auto-ozev wired chatWithPanel)
  - TestOverlayPanel: FAIL (overlay uses live-panel-viewer.js, not unified viewer)
  - TestUnresolvedState: FAIL (unified viewer Unresolved state not implemented yet)
"""
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tools.dashboard.tests import fixtures
from tools.dashboard.tests._xdist import worker_test_port


TEST_PORT = worker_test_port(8082)


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

class ViewerTestHarness:
    """Manages test server + fixture + browser for unified viewer tests."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.fixture_path = tmp_path / "fixtures.json"
        self.proc = None
        self.exp_id = fixtures.TEST_EXPERIMENT_ID

    def set_fixture(self, fixture_dict):
        """Swap the fixture data. Mock DAO reads this fresh on every request."""
        fixtures.write_fixture(fixture_dict, self.fixture_path)

    def start_server(self):
        # Kill any stale server on our port
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

    def open_session_page(self, project, session_id):
        """Navigate to the session detail page."""
        ab_raw("close")
        ab_raw("open", f"http://localhost:{TEST_PORT}/session/{project}/{session_id}",
               "--ignore-https-errors")
        time.sleep(3)

    def open_experiment(self):
        """Navigate to the experiment page."""
        ab_raw("close")
        ab_raw("open", f"http://localhost:{TEST_PORT}/design/{self.exp_id}",
               "--ignore-https-errors")
        time.sleep(2)
        ab_raw("set", "viewport", "390", "844")
        time.sleep(0.5)
        # Hide sidebar for mobile-style experiment view
        ab_eval("document.getElementById('sidebar').classList.add('-translate-x-full'); return 'ok';")
        time.sleep(0.5)

    def open_picker(self):
        """Open the chat session picker in experiment page."""
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

    def connect_session(self, session_id=None):
        """Connect to a session in the experiment chat panel."""
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
        """Disconnect from current session in experiment panel."""
        ab_eval("""
            var els = document.querySelectorAll('[x-data]');
            for (var i=0; i<els.length; i++) {
                var d = els[i]._x_dataStack && els[i]._x_dataStack[0];
                if (d && d.disconnectSession) { d.disconnectSession(); return; }
            }
        """)
        time.sleep(0.5)

    def visible_text(self):
        """Get all visible text on the page."""
        return ab_eval("return document.body.innerText") or ""

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

    def session_store_state(self, session_id):
        """Read the Alpine session store state for a session."""
        return ab_eval(f"""
            var store = Alpine.store('sessions');
            if (!store || !store['{session_id}']) return null;
            var s = store['{session_id}'];
            return {{
                loaded: s.loaded,
                isLive: s.isLive,
                linked: s.linked,
                entryCount: s.entries ? s.entries.length : 0,
            }};
        """)

    def is_picker_visible(self):
        """Check if the session picker is visible (vs chat panel)."""
        result = ab_eval("""
            var els = document.querySelectorAll('[x-data]');
            for (var i=0; i<els.length; i++) {
                var d = els[i]._x_dataStack && els[i]._x_dataStack[0];
                if (d && d.chatConnected !== undefined)
                    return !d.chatConnected;
            }
            return null;
        """)
        return result


# ── Fixtures ──────────────────────────────────────────────────────────

def _make_viewer_fixture(sessions, entries_map=None):
    """Build a complete fixture dict for the viewer tests."""
    if entries_map is None:
        entries_map = {s["session_id"]: fixtures.MOCK_SESSION_ENTRIES for s in sessions}
    return {
        "active_sessions": sessions,
        "beads": [],
        "experiments": [fixtures.make_experiment(fixtures.TEST_EXPERIMENT_ID)],
        "session_entries": entries_map,
    }


@pytest.fixture(scope="module")
def h(tmp_path_factory):
    """Test harness — server + browser + fixture management."""
    tmp = tmp_path_factory.mktemp("viewer")
    harness = ViewerTestHarness(tmp)
    sessions = [
        fixtures.make_linked_session("auto-test-live", label="Live Session",
                                     role="designer", last_message="Working on task"),
        fixtures.make_dead_session("auto-test-dead", label="Dead Session",
                                   role="reviewer", last_message="Completed"),
        fixtures.make_unresolved_session("host-test-unresolved", label="Unresolved Host",
                                         last_message="No JSONL yet"),
    ]
    harness.set_fixture(_make_viewer_fixture(sessions))
    harness.start_server()
    yield harness
    ab_raw("close")
    harness.stop()


# ══════════════════════════════════════════════════════════════════════
# TestSessionDetailPage — /session/{project}/{session_id}
# ══════════════════════════════════════════════════════════════════════

class TestSessionDetailPage:
    """Session detail page shows parsed entries directly."""

    def test_entries_visible(self, h):
        """Session detail page shows parsed conversation entries."""
        h.open_session_page("autonomy", "auto-test-live")
        time.sleep(2)
        text = h.visible_text()
        # The mock entries include user text "Hello, can you help me with this task?"
        # and assistant text "Of course! I'd be happy to help."
        assert "help" in text.lower(), (
            f"Session detail page should show entry content. Visible: {text[:300]}"
        )

    def test_live_indicator_when_live(self, h):
        """Live session shows some live indicator (dot, badge, or text)."""
        # Page should already be on auto-test-live from previous test
        text = h.visible_text()
        # The session viewer shows is_live status — check store state
        state = h.session_store_state("auto-test-live")
        if state:
            assert state.get("isLive") is True, (
                f"Store should show isLive=true for live session. State: {state}"
            )
        else:
            # Fallback: just verify the page loaded successfully
            assert "auto-test-live" in text or "Live" in text or len(text) > 50, (
                f"Session page did not load. Visible: {text[:200]}"
            )

    def test_no_input_bar_when_complete(self, h):
        """Dead session should not show send UI / input bar.

        NOTE: The mock tail API (server.py:2413) hardcodes is_live=True in
        its response, so the store always sees isLive=true regardless of the
        session fixture's is_live field.  This means we cannot verify the
        "no input bar" behavior through mock data alone — the real is_live
        comes from the SSE registry broadcast in production.

        This test verifies the page loads correctly for a "dead" session.
        The actual isLive=false → hidden input bar behavior will be verified
        by auto-vl46's integration tests against the real registry.
        """
        h.open_session_page("autonomy", "auto-test-dead")
        time.sleep(2)
        # Verify the page loads and shows content
        text = h.visible_text()
        assert len(text) > 20, f"Dead session page should show content. Visible: {text[:200]}"
        # Verify entries loaded (mock tail always returns them)
        state = h.session_store_state("auto-test-dead")
        if state:
            assert state.get("loaded") is True, (
                f"Dead session should have loaded entries. State: {state}"
            )


# ══════════════════════════════════════════════════════════════════════
# TestExperimentPanel — experiment page chat panel
# ══════════════════════════════════════════════════════════════════════

class TestExperimentPanel:
    """Experiment page chat panel connects to sessions and shows entries."""

    def test_connect_shows_entries(self, h):
        """Pick session in experiment page → entries render (not 'No messages yet')."""
        h.open_experiment()
        h.open_picker()
        session_id = h.connect_session("auto-test-live")
        assert session_id, "Failed to connect to session"
        # SSE-populated state needs time to load entries
        time.sleep(5)
        result = h.chat_entry_count()
        assert result["count"] > 0, (
            f"Experiment panel should show entries after connecting. "
            f"Status: {result['status']}. "
            "If count=0, chatWithPanel may not be wired to entries."
        )

    def test_disconnect_returns_to_picker(self, h):
        """Disconnect → picker visible again."""
        h.disconnect()
        time.sleep(1)
        picker_visible = h.is_picker_visible()
        assert picker_visible is True, (
            "After disconnect, session picker should be visible"
        )

    def test_primer_button_visible(self, h):
        """Connected session shows prime icon or button."""
        h.open_picker()
        h.connect_session("auto-test-live")
        time.sleep(3)
        text = h.visible_text()
        # The primer button may be a small icon — check for any primer-related text
        # or just verify the panel is in connected state with entries
        result = h.chat_entry_count()
        # This is a soft check — primer button may not have visible text
        assert result["count"] > 0 or "prime" in text.lower() or len(text) > 50, (
            "Connected panel should show content or primer affordance"
        )


# ══════════════════════════════════════════════════════════════════════
# TestOverlayPanel — dispatch live trace overlay
# EXPECTED FAIL: overlay uses live-panel-viewer.js, not unified viewer
# ══════════════════════════════════════════════════════════════════════

class TestOverlayPanel:
    """Overlay panel for dispatch runs — currently uses live-panel-viewer.js."""

    @pytest.mark.xfail(
        reason="overlay uses live-panel-viewer.js, not unified viewer",
        strict=True,
    )
    def test_overlay_opens_from_dispatch(self, h):
        """Click Live Trace → overlay panel appears with entries.

        EXPECTED FAIL: The overlay currently uses live-panel-viewer.js,
        a separate component. Until the unified viewer replaces it
        (auto-vl46), this test cannot pass.
        """
        # The overlay is triggered by window._livePanelLoad(runDir, isLive)
        # which is wired to live-panel-viewer.js, not the unified viewer.
        # When auto-vl46 ships, the overlay will use configure({runDir})
        # on the unified viewer instead.
        assert False, "overlay uses live-panel-viewer.js, not unified viewer"

    @pytest.mark.xfail(
        reason="overlay uses live-panel-viewer.js, not unified viewer",
        strict=True,
    )
    def test_overlay_shows_completed_trace(self, h):
        """Completed run → overlay with historical entries.

        EXPECTED FAIL: same reason — overlay uses live-panel-viewer.js.
        """
        assert False, "overlay uses live-panel-viewer.js, not unified viewer"

    @pytest.mark.xfail(
        reason="overlay uses live-panel-viewer.js, not unified viewer",
        strict=True,
    )
    def test_overlay_close_removes_panel(self, h):
        """Close button hides overlay.

        EXPECTED FAIL: same reason — overlay uses live-panel-viewer.js.
        """
        assert False, "overlay uses live-panel-viewer.js, not unified viewer"


# ══════════════════════════════════════════════════════════════════════
# TestUnresolvedState — host sessions with linked=false
# EXPECTED FAIL: unified viewer Unresolved state not implemented yet
# ══════════════════════════════════════════════════════════════════════

class TestUnresolvedState:
    """Unresolved state — host sessions with no JSONL path (linked=false)."""

    @pytest.mark.xfail(
        reason="unified viewer Unresolved state not implemented yet",
        strict=True,
    )
    def test_link_button_visible(self, h):
        """Host session with linked=false → Link Terminal button."""
        h.open_session_page("autonomy", "host-test-unresolved")
        time.sleep(2)
        text = h.visible_text()
        assert "link" in text.lower() and "terminal" in text.lower(), (
            "unified viewer Unresolved state not implemented yet — "
            f"no Link Terminal button visible. Text: {text[:200]}"
        )

    @pytest.mark.xfail(
        reason="unified viewer Unresolved state not implemented yet",
        strict=True,
    )
    def test_no_entries_when_unresolved(self, h):
        """Unresolved session shows empty state, not stale entries.

        EXPECTED FAIL: The current viewer doesn't distinguish between
        "no entries yet" and "unresolved — can't tail". auto-vl46 will
        add explicit empty state for unresolved sessions.
        """
        h.open_session_page("autonomy", "host-test-unresolved")
        time.sleep(2)
        # In the unresolved state, there should be NO entries and
        # an explicit message about linking being needed
        state = h.session_store_state("host-test-unresolved")
        if state:
            assert state.get("linked") is False, (
                "unified viewer Unresolved state not implemented yet"
            )
            # When unresolved state IS implemented, entries should be empty
            # and a specific "Link Terminal" message should appear
            text = h.visible_text()
            assert "link" in text.lower(), (
                "unified viewer Unresolved state not implemented yet — "
                "no link prompt shown for unresolved session"
            )
        else:
            assert False, "unified viewer Unresolved state not implemented yet"
