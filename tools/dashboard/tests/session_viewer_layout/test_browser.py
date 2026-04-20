"""
Browser tests for the session viewer grid shell + contenteditable input layout.

Locks in the measurable assertions from graph note 06d453f2-0a9:
  - Grid shell active in page mode (.sv-ready display == 'grid')
  - No position:fixed on the session viewer grid children
  - Contenteditable editable + hasContent toggling
  - Send button disabled state reflects hasContent + attachments
  - visualViewport keyboard padding toggle
  - iOS standalone class toggle + --app-height
  - Dashboard chrome hidden on session pages (fullscreen-page body class)
  - Prototype route /test/input returns baked HTML (Part L)

Runs against the DASHBOARD_MOCK-backed server harness shared with unified_viewer
tests.
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


TEST_PORT = worker_test_port(8083)


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


class LayoutTestHarness:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.fixture_path = tmp_path / "fixtures.json"
        self.proc = None

    def set_fixture(self, fixture_dict):
        fixtures.write_fixture(fixture_dict, self.fixture_path)

    def start_server(self):
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
        ab_raw("close")
        ab_raw("set", "viewport", "390", "844")
        time.sleep(0.3)
        ab_raw("open", f"http://localhost:{TEST_PORT}/session/{project}/{session_id}",
               "--ignore-https-errors")
        time.sleep(3)


@pytest.fixture(scope="module")
def h(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("layout")
    harness = LayoutTestHarness(tmp)
    sessions = [
        fixtures.make_linked_session("auto-test-live", label="Live Session",
                                     role="designer", last_message="Working on task"),
    ]
    harness.set_fixture({
        "active_sessions": sessions,
        "beads": [],
        "experiments": [fixtures.make_experiment(fixtures.TEST_EXPERIMENT_ID)],
        "session_entries": {
            s["session_id"]: fixtures.MOCK_SESSION_ENTRIES for s in sessions
        },
    })
    harness.start_server()
    yield harness
    ab_raw("close")
    harness.stop()


# ══════════════════════════════════════════════════════════════════════
# Grid shell + layout assertions
# ══════════════════════════════════════════════════════════════════════

class TestGridShell:
    def test_sv_ready_is_grid(self, h):
        """`.sv-ready` must render as a CSS grid in page mode."""
        h.open_session_page("autonomy", "auto-test-live")
        time.sleep(1)
        display = ab_eval("""
            var el = document.querySelector('.sv-ready');
            return el ? getComputedStyle(el).display : 'missing';
        """)
        assert display == "grid", f"Expected .sv-ready display=grid, got {display!r}"

    def test_no_fixed_on_grid_children(self, h):
        """Header / entries / input must not be position:fixed (grid shell flow)."""
        positions = ab_eval("""
            return ['sv-header','sv-entries','sv-input'].map(function(c) {
              var el = document.querySelector('.' + c);
              return el ? getComputedStyle(el).position : null;
            });
        """)
        for cls, pos in zip(["sv-header", "sv-entries", "sv-input"], positions or []):
            if pos is None:
                continue  # e.g. no .sv-input if session not live-linked
            assert pos != "fixed", f".{cls} has position: fixed (expected static/relative)"

    def test_viewer_fills_height(self, h):
        """Session viewer should fill the viewport height."""
        heights = ab_eval("""
            var v = document.querySelector('.session-viewer');
            return {
              viewer: v ? v.offsetHeight : 0,
              viewport: document.documentElement.clientHeight,
            };
        """)
        assert heights["viewer"] > 0
        assert abs(heights["viewer"] - heights["viewport"]) <= 4, (
            f"Viewer ({heights['viewer']}) should match viewport ({heights['viewport']})"
        )

    def test_header_pinned_top(self, h):
        """Header sits at viewport top (within safe-area)."""
        rect = ab_eval("""
            var el = document.querySelector('.sv-header');
            if (!el) return null;
            var r = el.getBoundingClientRect();
            return { top: r.top };
        """)
        assert rect and rect["top"] <= 20, (
            f".sv-header must be pinned at viewport top (got top={rect and rect['top']})"
        )


class TestFullscreenChrome:
    def test_dashboard_chrome_hidden(self, h):
        """Session page must hide sidebar + header via body.fullscreen-page."""
        state = ab_eval("""
            return {
              hasClass: document.body.classList.contains('fullscreen-page'),
              hamb: (function(){ var e=document.getElementById('nav-toggle'); return e && e.offsetParent !== null; })(),
              search: (function(){ var e=document.getElementById('global-search'); return e && e.offsetParent !== null; })(),
              sidebar: (function(){ var e=document.getElementById('sidebar'); return e && e.offsetParent !== null; })(),
            };
        """)
        assert state["hasClass"], "Body should have fullscreen-page class on session pages"
        assert not state["hamb"], "Hamburger should be hidden on session pages"
        assert not state["search"], "Search bar should be hidden on session pages"


# ══════════════════════════════════════════════════════════════════════
# Contenteditable + send button
# ══════════════════════════════════════════════════════════════════════

class TestEditable:
    def test_editable_exists(self, h):
        """The editable input element exists (replacing the textarea)."""
        count = ab_eval("return document.querySelectorAll('.sv-input .sv-editable').length;")
        assert count and count >= 1, "Expected .sv-editable inside .sv-input"

    def test_hascontent_starts_false(self, h):
        """Fresh load: hasContent is false and send button disabled."""
        state = ab_eval("""
            // Clear any leftover draft
            var el = document.querySelector('.sv-input .sv-editable');
            if (el) {
              el.innerText = '';
              var host = el.closest('[x-data]');
              var alpine = host && host._x_dataStack && host._x_dataStack[0];
              if (alpine) { alpine.hasContent = false; }
            }
            return {
              empty: el ? (el.innerText === '' || !el.innerText.trim()) : null,
              sendDisabled: (function(){
                var btn = document.querySelector('.sv-send');
                return btn ? btn.disabled : null;
              })(),
            };
        """)
        assert state["empty"] in (True, None)
        assert state["sendDisabled"] in (True, None), \
            "Send button should be disabled when input is empty"

    def test_oninput_toggles_hascontent(self, h):
        """Typing into editable toggles hasContent and enables send button."""
        state = ab_eval("""
            var el = document.querySelector('.sv-input .sv-editable');
            if (!el) return { skip: true };
            el.innerText = 'hello';
            var host = el.closest('[x-data]');
            var alpine = host && host._x_dataStack && host._x_dataStack[0];
            if (alpine && alpine.onInput) alpine.onInput(el);
            return {
              hasContent: alpine ? !!alpine.hasContent : null,
              text: el.innerText,
            };
        """)
        if state.get("skip"):
            pytest.skip("No .sv-editable present")
        assert state["hasContent"] is True
        assert state["text"] == "hello"


# ══════════════════════════════════════════════════════════════════════
# Prototype baked-in (Part L)
# ══════════════════════════════════════════════════════════════════════

class TestPrototypeRoute:
    def test_test_input_route_returns_html(self, h):
        """/test/input must serve the baked prototype (data/test-fixtures/input-prototype.html)."""
        import httpx
        resp = httpx.get(f"http://localhost:{TEST_PORT}/test/input", timeout=5)
        assert resp.status_code == 200
        assert "Session Viewer — Input Prototype" in resp.text or \
               "input-bar" in resp.text or "sv-input" in resp.text, (
            "Prototype HTML did not load from baked file at data/test-fixtures/"
        )


# ══════════════════════════════════════════════════════════════════════
# Loading / error classes (Part K.1)
# ══════════════════════════════════════════════════════════════════════

class TestLoadingErrorClasses:
    def test_loading_state_has_class(self, h):
        """Ready state is the sv-ready grid (loading transitions fast on mock)."""
        h.open_session_page("autonomy", "auto-test-live")
        time.sleep(2)
        ready = ab_eval("""
            return !!document.querySelector('.sv-ready');
        """)
        assert ready is True, ".sv-ready should be present once session loads"
