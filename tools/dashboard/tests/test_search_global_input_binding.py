"""Browser smoke test for the global search input two-way binding (auto-zvu3z).

The global "Search graph…" input in the top header is now the canonical
query input. Two contracts:

  - On /search: typing into the global input dispatches
    ``global-search:input`` → the search page's Alpine root listens, binds
    to ``query``, debounces a refetch, and replaceState()s the URL. Enter
    dispatches ``global-search:enter`` so the page can flush the debounce.
  - Off /search: existing nav behaviour — Enter navigates to /search?q=…

This test boots a real uvicorn process against a DASHBOARD_MOCK fixture
and exercises both routes through agent-browser at iPhone width.

Skipped when the ``agent-browser`` binary is not available.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tools.dashboard.tests._xdist import worker_test_port


TEST_PORT = worker_test_port(8123)


def _has_agent_browser() -> bool:
    return shutil.which("agent-browser") is not None


pytestmark = pytest.mark.skipif(
    not _has_agent_browser(),
    reason="agent-browser binary not available",
)


# ── agent-browser helpers (cribbed from test_search_smoke.py) ─────────


def ab(*args, stdin_text=None, timeout=10):
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


def _fixture():
    return {
        "active_sessions": [],
        "beads": [],
        "search_results": [
            {"id": "row-1", "source_id": "src-foo-1",
             "source_title": "Foo session",
             "source_type": "session", "result_type": "thought",
             "project": "autonomy", "platform": "claude-code",
             "turn_number": 1, "rank": -3.0,
             "content": "matching foo content",
             "source_created_at": "2026-04-20T03:14:58Z"},
            {"id": "row-2", "source_id": "src-bar-1",
             "source_title": "Bar note",
             "source_type": "note", "result_type": "thought",
             "project": "autonomy", "platform": "local",
             "turn_number": None, "rank": -2.0,
             "content": "matching bar content",
             "source_created_at": "2026-04-19T00:00:00Z"},
        ],
    }


class GlobalInputHarness:

    def __init__(self, tmp_path):
        self.fixture_path = tmp_path / "global-input-fixture.json"
        self.events_file = tmp_path / "events.jsonl"
        self.events_file.touch()
        self.proc = None

    def write_fixture(self, data):
        self.fixture_path.write_text(json.dumps(data, indent=2))

    def start(self):
        subprocess.run(
            ["pkill", "-f", f"uvicorn.*{TEST_PORT}"],
            capture_output=True, timeout=3,
        )
        time.sleep(0.5)
        env = os.environ.copy()
        env["DASHBOARD_MOCK"] = str(self.fixture_path)
        env["DASHBOARD_MOCK_EVENTS"] = str(self.events_file)
        repo_root = str(Path(__file__).resolve().parents[3])
        env["PYTHONPATH"] = repo_root
        self.proc = subprocess.Popen(
            ["python3", "-m", "uvicorn", "tools.dashboard.server:app",
             "--host", "127.0.0.1", "--port", str(TEST_PORT)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=repo_root,
        )
        import httpx
        for _ in range(40):
            try:
                if httpx.get(
                    f"http://localhost:{TEST_PORT}/search?q=foo",
                    timeout=1,
                ).status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(0.5)
        self.stop()
        raise RuntimeError("Global-input smoke server failed to start")

    def stop(self):
        if self.proc:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("global-input-smoke")
    h = GlobalInputHarness(tmp)
    h.write_fixture(_fixture())
    h.start()
    yield h
    ab_raw("close")
    h.stop()


def _open(path):
    ab_raw("close")
    ab_raw(
        "open",
        f"http://localhost:{TEST_PORT}{path}",
        "--ignore-https-errors",
    )
    time.sleep(2.5)
    ab_raw("set", "viewport", "390", "844")
    time.sleep(0.5)


# ── 1. /search — input event drives the page's query ──────────────────


class TestGlobalInputOnSearchPage:

    def test_global_input_dispatches_event_and_updates_url(self, harness):
        """Typing into #global-search on /search dispatches a custom
        event the page binds to query + replaceState the URL."""
        _open("/search?q=foo")
        # Reset the global input to a fresh value, dispatch an input
        # event (simulating a keystroke), then read both: the URL and the
        # page's Alpine query ref.
        result = ab_eval("""
            var gs = document.getElementById('global-search');
            if (!gs) return {error: 'no #global-search'};
            gs.value = 'bar';
            gs.dispatchEvent(new Event('input', { bubbles: true }));
            // Wait past the 300ms debounce so the URL replaceState has fired.
            return new Promise(function(resolve) {
                setTimeout(function() {
                    var url = new URL(window.location.href);
                    var root = document.querySelector('[x-data^="searchPage"]');
                    var query = root && root._x_dataStack
                        ? root._x_dataStack[0].query
                        : null;
                    resolve({ q_param: url.searchParams.get('q'), query: query });
                }, 500);
            });
        """)
        assert result, "eval returned nothing"
        assert result.get("q_param") == "bar", (
            f"URL q= should reflect the typed value, got {result!r}"
        )
        assert result.get("query") == "bar", (
            f"Alpine query ref should bind to the global input, got {result!r}"
        )

    def test_global_input_does_not_navigate(self, harness):
        """While on /search, typing or pressing Enter must NOT navigate to a
        new path — the URL stays at /search and only q= changes."""
        _open("/search?q=foo")
        result = ab_eval("""
            var path_before = window.location.pathname;
            var gs = document.getElementById('global-search');
            gs.value = 'baz';
            gs.dispatchEvent(new Event('input', { bubbles: true }));
            var ev = new KeyboardEvent('keydown', {
                key: 'Enter', bubbles: true, cancelable: true
            });
            gs.dispatchEvent(ev);
            return new Promise(function(resolve) {
                setTimeout(function() {
                    resolve({
                        path_before: path_before,
                        path_after: window.location.pathname,
                        q_param: new URL(window.location.href).searchParams.get('q'),
                    });
                }, 400);
            });
        """)
        assert result, "eval returned nothing"
        assert result.get("path_before") == "/search"
        assert result.get("path_after") == "/search", (
            f"global input must not navigate while on /search, got {result!r}"
        )
        assert result.get("q_param") == "baz"

    def test_global_input_initial_value_synced_from_url_q(self, harness):
        """Landing on /search?q=foo populates #global-search with ``foo`` —
        the chrome's canonical query input always reflects the live q."""
        _open("/search?q=foo")
        # Allow the Alpine init() to run and call _syncGlobalInput().
        time.sleep(0.5)
        gs_value = ab_eval("""
            var gs = document.getElementById('global-search');
            return gs ? gs.value : null;
        """)
        assert gs_value == "foo", (
            f"expected #global-search.value='foo' on landing, got {gs_value!r}"
        )


# ── 2. Off /search — Enter still navigates to /search?q=… ─────────────


class TestGlobalInputOffSearchPage:

    def test_enter_off_search_navigates_to_search(self, harness):
        """Press Enter in #global-search while on /sessions → URL changes
        to /search?q=…. This is the existing nav behaviour from auto-bcxdr,
        the new wiring must not break it."""
        _open("/sessions")
        # Wait for the SPA to render before driving the input.
        time.sleep(1.0)
        ab_eval("""
            var gs = document.getElementById('global-search');
            gs.value = 'navtest';
            gs.dispatchEvent(new Event('input', { bubbles: true }));
            var ev = new KeyboardEvent('keydown', {
                key: 'Enter', bubbles: true, cancelable: true
            });
            gs.dispatchEvent(ev);
            return true;
        """)
        # Wait a beat for navigateTo() to push the new URL + render.
        time.sleep(1.0)
        url_info = ab_eval("""
            var u = new URL(window.location.href);
            return { path: u.pathname, q: u.searchParams.get('q') };
        """)
        assert url_info, "eval returned nothing after Enter"
        assert url_info.get("path") == "/search", (
            f"Enter off /search should navigate to /search, got {url_info!r}"
        )
        assert url_info.get("q") == "navtest", (
            f"q param should carry the typed value, got {url_info!r}"
        )

    def test_enter_off_search_initializes_search_page_alpine(self, harness):
        """Navigating into /search from another page must still initialize
        the search fragment's Alpine root."""
        _open("/sessions")
        time.sleep(1.0)
        ab_eval("""
            var gs = document.getElementById('global-search');
            gs.value = 'navinit';
            gs.dispatchEvent(new Event('input', { bubbles: true }));
            gs.dispatchEvent(new KeyboardEvent('keydown', {
                key: 'Enter', bubbles: true, cancelable: true
            }));
            return true;
        """)
        time.sleep(1.0)
        result = ab_eval("""
            var u = new URL(window.location.href);
            var root = document.querySelector('[x-data^="searchPage"]');
            var data = root && root._x_dataStack ? root._x_dataStack[0] : null;
            return {
                path: u.pathname,
                q: u.searchParams.get('q'),
                initialized: !!data,
                query: data ? data.query : null,
                loaded: data ? data.loaded : null
            };
        """)
        assert result, "eval returned nothing after cross-page search nav"
        assert result.get("path") == "/search", (
            f"expected /search after Enter nav, got {result!r}"
        )
        assert result.get("q") == "navinit", (
            f"q param should carry the typed value, got {result!r}"
        )
        assert result.get("initialized") is True, (
            f"search Alpine root failed to initialize after SPA nav, got {result!r}"
        )
        assert result.get("query") == "navinit", (
            f"search Alpine state should hydrate from URL after SPA nav, got {result!r}"
        )
