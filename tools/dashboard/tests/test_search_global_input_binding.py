"""Browser behavior tests for the compact global search control.

The global "Search graph…" input in the top header is now the canonical
query input. Two contracts:

  - On /search: typing into the global input dispatches
    ``global-search:input`` → the search page's Alpine root listens, binds
    to ``query``, debounces a refetch, and replaceState()s the URL. Enter
    dispatches ``global-search:enter`` so the page can flush the debounce.
  - Off /search: existing nav behaviour — Enter navigates to /search?q=…

The shared shell keeps the canonical input collapsed behind a magnifier and
pins personal identity at the right edge. Search mode temporarily owns the
whole bar, while immersive and app-owned surfaces expose neither utility.

These tests boot a real uvicorn process against a DASHBOARD_MOCK fixture and
exercise the shell through agent-browser at iPhone and desktop widths.

Skipped when the ``agent-browser`` binary is not available.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import uuid
import time
from pathlib import Path

import pytest

from tools.dashboard.tests._xdist import spawn_mock_uvicorn, worker_test_port


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
        self.nonce = uuid.uuid4().hex

    def write_fixture(self, data):
        self.fixture_path.write_text(
            json.dumps({**data, "__harness_nonce__": self.nonce}, indent=2)
        )

    def start(self):
        # OS-assigned port via --fd + nonce identity check — worker_test_port is
        # worker-index derived with no session dimension (port-collision report,
        # auto-0812-211339).
        global TEST_PORT
        env = os.environ.copy()
        env["DASHBOARD_MOCK"] = str(self.fixture_path)
        env["DASHBOARD_MOCK_EVENTS"] = str(self.events_file)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3])
        self.proc, TEST_PORT = spawn_mock_uvicorn(env=env, nonce=self.nonce)

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
    ab_raw(
        "open",
        f"http://localhost:{TEST_PORT}{path}",
        "--ignore-https-errors",
    )
    time.sleep(2.5)
    ab_raw("set", "viewport", "390", "844")
    time.sleep(0.5)


# ── 1. Compact shell lifecycle ────────────────────────────────────────


class TestCompactGlobalSearchChrome:

    def test_mobile_starts_collapsed_with_profile_pinned_right(self, harness):
        _open("/sessions")
        state = ab_eval("""
            var header = document.querySelector('header[data-app-chrome]');
            var control = document.getElementById('global-search-control');
            var input = document.getElementById('global-search');
            var profile = document.getElementById('identity-indicator');
            var icon = document.getElementById('global-search-icon');
            var menu = document.getElementById('nav-toggle');
            var inbox = document.querySelector(
                '[data-testid="central-attention-button"]');
            var hr = header.getBoundingClientRect();
            var pr = profile.getBoundingClientRect();
            function shape(el) {
                var rect = el.getBoundingClientRect();
                var css = getComputedStyle(el);
                return {
                    tag: el.tagName,
                    width: rect.width,
                    height: rect.height,
                    radius: css.borderTopLeftRadius,
                    appearance: css.appearance,
                    borders: [
                        css.borderTopWidth + ' ' + css.borderTopStyle + ' ' + css.borderTopColor,
                        css.borderRightWidth + ' ' + css.borderRightStyle + ' ' + css.borderRightColor,
                        css.borderBottomWidth + ' ' + css.borderBottomStyle + ' ' + css.borderBottomColor,
                        css.borderLeftWidth + ' ' + css.borderLeftStyle + ' ' + css.borderLeftColor,
                    ],
                    background: css.backgroundColor,
                };
            }
            var shapes = [shape(menu), shape(inbox), shape(icon)];
            return {
                collapsed: !header.classList.contains('global-search-open') &&
                    !control.classList.contains('is-open'),
                input_hidden: input.getAttribute('aria-hidden') === 'true' &&
                    input.tabIndex === -1 && input.getBoundingClientRect().width <= 1,
                icon_visible: icon.offsetParent !== null,
                profile_visible: profile.offsetParent !== null,
                profile_on_right: Math.abs(hr.right - pr.right) <= 18,
                no_overflow: document.documentElement.scrollWidth <= window.innerWidth + 1,
                toolbar_icons_uniform: shapes.every(function(value) {
                    return Math.abs(value.width - shapes[0].width) <= 0.5 &&
                        Math.abs(value.height - shapes[0].height) <= 0.5 &&
                        value.radius === shapes[0].radius &&
                        value.tag === shapes[0].tag &&
                        value.appearance === shapes[0].appearance &&
                        JSON.stringify(value.borders) === JSON.stringify(shapes[0].borders) &&
                        value.background === shapes[0].background;
                }),
                toolbar_icon_shape: shapes[0],
            };
        """)
        assert state == {
            "collapsed": True,
            "input_hidden": True,
            "icon_visible": True,
            "profile_visible": True,
            "profile_on_right": True,
            "no_overflow": True,
            "toolbar_icons_uniform": True,
            "toolbar_icon_shape": {
                "tag": "BUTTON",
                "width": 40,
                "height": 40,
                "radius": "8px",
                "appearance": "none",
                "borders": [
                    "1px solid rgb(55, 65, 81)",
                    "1px solid rgb(55, 65, 81)",
                    "1px solid rgb(55, 65, 81)",
                    "1px solid rgb(55, 65, 81)",
                ],
                "background": "rgb(31, 41, 55)",
            },
        }

    def test_open_search_owns_bar_and_toggle_restores_shell(self, harness):
        _open("/sessions")
        state = ab_eval("""
            var header = document.querySelector('header[data-app-chrome]');
            var icon = document.getElementById('global-search-icon');
            var input = document.getElementById('global-search');
            icon.click();
            return new Promise(function(resolve) {
                requestAnimationFrame(function() { requestAnimationFrame(function() {
                    var open = {
                        active: header.classList.contains('global-search-open'),
                        focused: document.activeElement === input,
                        expanded: icon.getAttribute('aria-expanded') === 'true',
                        nav_hidden: document.getElementById('nav-toggle').offsetParent === null,
                        attention_hidden: document.querySelector('[data-testid="central-attention-root"]').offsetParent === null,
                        profile_hidden: document.getElementById('identity-indicator').offsetParent === null,
                        no_overflow: document.documentElement.scrollWidth <= window.innerWidth + 1,
                    };
                    icon.click();
                    requestAnimationFrame(function() {
                        resolve({
                            open: open,
                            closed: !header.classList.contains('global-search-open') &&
                                icon.getAttribute('aria-expanded') === 'false' &&
                                document.getElementById('identity-indicator').offsetParent !== null,
                        });
                    });
                }); });
            });
        """)
        assert state["open"] == {
            "active": True,
            "focused": True,
            "expanded": True,
            "nav_hidden": True,
            "attention_hidden": True,
            "profile_hidden": True,
            "no_overflow": True,
        }
        assert state["closed"] is True

    def test_mobile_shell_controls_are_mutually_exclusive(self, harness):
        _open("/sessions")
        state = ab_eval("""
            var header = document.querySelector('header[data-app-chrome]');
            var sidebar = document.getElementById('sidebar');
            var nav = document.getElementById('nav-toggle');
            var attentionRoot = document.querySelector(
                '[data-testid="central-attention-root"]');
            var inbox = attentionRoot.querySelector(
                '[data-testid="central-attention-button"]');
            var attention = Alpine.$data(attentionRoot);
            var search = document.getElementById('global-search-icon');
            inbox.click();
            return new Promise(function(resolve) {
                requestAnimationFrame(function() { requestAnimationFrame(function() {
                    var inboxOwnsShell = attention.inboxOpen &&
                        sidebar.classList.contains('-translate-x-full') &&
                        !header.classList.contains('global-search-open');
                    search.click();
                    requestAnimationFrame(function() { requestAnimationFrame(function() {
                        var searchOwnsShell = !attention.inboxOpen &&
                            sidebar.classList.contains('-translate-x-full') &&
                            header.classList.contains('global-search-open');
                        nav.click();
                        requestAnimationFrame(function() { requestAnimationFrame(function() {
                            var navOwnsShell = !attention.inboxOpen &&
                                !header.classList.contains('global-search-open') &&
                                !sidebar.classList.contains('-translate-x-full') &&
                                getComputedStyle(attentionRoot).display === 'none' &&
                                nav.classList.contains('toolbar-icon-button-active') &&
                                nav.getAttribute('aria-expanded') === 'true';
                            nav.click();
                            requestAnimationFrame(function() {
                                resolve({
                                    inbox_owns_shell: inboxOwnsShell,
                                    search_owns_shell: searchOwnsShell,
                                    nav_owns_shell: navOwnsShell,
                                    close_restores_default:
                                        sidebar.classList.contains('-translate-x-full') &&
                                        getComputedStyle(attentionRoot).display !== 'none' &&
                                        !nav.classList.contains('toolbar-icon-button-active') &&
                                        nav.getAttribute('aria-expanded') === 'false',
                                });
                            });
                        }); });
                    }); });
                }); });
            });
        """)
        assert state == {
            "inbox_owns_shell": True,
            "search_owns_shell": True,
            "nav_owns_shell": True,
            "close_restores_default": True,
        }

    def test_escape_collapses_and_returns_focus_to_magnifier(self, harness):
        _open("/sessions")
        state = ab_eval("""
            var header = document.querySelector('header[data-app-chrome]');
            var icon = document.getElementById('global-search-icon');
            var input = document.getElementById('global-search');
            icon.click();
            return new Promise(function(resolve) {
                requestAnimationFrame(function() { requestAnimationFrame(function() {
                    input.dispatchEvent(new KeyboardEvent('keydown', {
                        key: 'Escape', bubbles: true, cancelable: true
                    }));
                    resolve({
                        collapsed: !header.classList.contains('global-search-open'),
                        focused_icon: document.activeElement === icon,
                    });
                }); });
            });
        """)
        assert state == {"collapsed": True, "focused_icon": True}

    def test_desktop_profile_and_search_fit_without_overflow(self, harness):
        _open("/sessions")
        ab_raw("set", "viewport", "1280", "800")
        time.sleep(0.4)
        state = ab_eval("""
            var profile = document.getElementById('identity-indicator');
            var icon = document.getElementById('global-search-icon');
            return {
                profile_visible: profile.offsetParent !== null,
                icon_visible: icon.offsetParent !== null,
                no_overflow: document.documentElement.scrollWidth <= window.innerWidth + 1,
            };
        """)
        assert state == {
            "profile_visible": True,
            "icon_visible": True,
            "no_overflow": True,
        }

    def test_app_owned_and_session_routes_hide_shell_utilities(self, harness):
        _open("/sessions")
        state = ab_eval("""
            var header = document.querySelector('header[data-app-chrome]');
            window.Autonomy.setTopbar({html: '<div>App tools</div>'});
            var appOwned = {
                search_hidden: document.getElementById('global-search-control').offsetParent === null,
                profile_hidden: document.getElementById('identity-indicator').offsetParent === null,
            };
            window.Autonomy.resetTopbar();
            window.history.pushState({}, '', '/session/autonomy/not-running');
            return route().then(function() {
                return new Promise(function(resolve) {
                  setTimeout(function() {
                    resolve({
                        app_owned: appOwned,
                        route_immersive: document.body.classList.contains('route-immersive'),
                        search_hidden: document.getElementById('global-search-control').offsetParent === null,
                        profile_hidden: document.getElementById('identity-indicator').offsetParent === null,
                        header_present: !!header,
                    });
                  }, 100);
                });
            });
        """)
        assert state["app_owned"] == {
            "search_hidden": True,
            "profile_hidden": True,
        }
        assert state["route_immersive"] is True
        assert state["search_hidden"] is True
        assert state["profile_hidden"] is True
        assert state["header_present"] is True


# ── 2. /search — input event drives the page's query ──────────────────


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
            document.getElementById('global-search-icon').click();
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
                        collapsed: !document.querySelector('header[data-app-chrome]')
                            .classList.contains('global-search-open'),
                        profile_visible: document.getElementById('identity-indicator')
                            .offsetParent !== null,
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
        assert result.get("collapsed") is True
        assert result.get("profile_visible") is True

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


# ── 3. Off /search — Enter still navigates to /search?q=… ─────────────


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
