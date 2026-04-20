"""
Phase 0 — Consolidation UI: L2 browser tests.

Bead auto-ylj6r. Browser-level tests that exercise the dispatch overlay,
Recent-card click routing, and the Active list filter for dispatch/
librarian sessions. All MUST FAIL on master.

Covered tests from the bead's Phase 0 test table:
  #12 test_overlay_updates_live_on_running_dispatch
  #13 test_recent_dispatch_card_click_resolves_viewer
  #14 test_recent_librarian_card_click_resolves_viewer
  #15 test_no_phantom_after_recent_click_back_nav
  #20 test_dispatch_not_in_active_list
  #21 test_librarian_not_in_active_list

These use the mock-fixture harness (SessionsTestHarness + agent-browser),
matching the pattern in test_browser.py / test_nag_bell_states.py.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from tools.dashboard.tests import fixtures
from tools.dashboard.tests.fixtures import make_session
from tools.dashboard.tests.sessions.test_browser import (
    TEST_PORT,
    SessionsTestHarness,
    ab_eval,
    ab_raw,
)


def _iso(minutes_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ── Fixture: sessions mix covering dispatch/librarian/interactive ─────


_DISPATCH_TMUX = "auto-disp-0420-120000"
_DISPATCH_UUID = "dddddddd-1111-2222-3333-444444444444"
_DISP_BEAD = "auto-disptest"
_LIB_TMUX = "auto-lib-0420-120100"
_LIB_UUID = "eeeeeeee-1111-2222-3333-444444444444"
_LIVE_INTERACTIVE = "auto-live-interactive"


_ACTIVE_SESSIONS = [
    # Interactive (must show on Active list)
    {**make_session(_LIVE_INTERACTIVE, label="Interactive live",
                    role="designer", last_message="hello"),
     "type": "container"},
    # Dispatch — live but must NOT appear on Active list (bead Phase 5)
    {**make_session(_DISPATCH_TMUX, label="Running dispatch",
                    role="dispatch", last_message="working"),
     "type": "dispatch", "tmux_session": _DISPATCH_TMUX,
     "session_id": _DISPATCH_TMUX},
    # Librarian — live but must NOT appear on Active list
    {**make_session(_LIB_TMUX, label="Running librarian",
                    role="librarian", last_message="indexing"),
     "type": "librarian", "tmux_session": _LIB_TMUX,
     "session_id": _LIB_TMUX},
]


_RECENT_SESSIONS = [
    # Recent dispatch row — fixture mimics graph.db output
    {
        "id": "src-dispatch-recent-001",
        "type": "session",
        "date": "2026-04-20",
        "title": "Recent dispatch: Fix X",
        "project": "autonomy",
        "session_uuid": _DISPATCH_UUID,
        "tmux_session": _DISPATCH_TMUX,
        "session_type": "dispatch",
        "bead_id": _DISP_BEAD,
        "entry_count": 12,
        "context_tokens": 34000,
        "last_activity_at": _iso(5),
        "created_at": _iso(30),
        "ended_at": _iso(5),
    },
    # Recent librarian row
    {
        "id": "src-librarian-recent-001",
        "type": "session",
        "date": "2026-04-20",
        "title": "librarian-review_report-123-abc",
        "project": "autonomy",
        "session_uuid": _LIB_UUID,
        "tmux_session": _LIB_TMUX,
        "session_type": "librarian",
        "librarian_type": "review_report",
        "librarian_target_bead_id": "auto-tgt",
        "librarian_target_bead_title": "Target Bead",
        "entry_count": 8,
        "context_tokens": 22000,
        "last_activity_at": _iso(10),
        "created_at": _iso(40),
    },
]


_SESSION_ENTRIES = {
    _DISPATCH_TMUX: [
        {"type": "user", "content": "dispatch start", "timestamp": 1700000000},
        {"type": "assistant_text", "content": "dispatch response", "timestamp": 1700000010},
    ],
    _LIB_TMUX: [
        {"type": "system", "content": "librarian boot", "timestamp": 1700000000},
        {"type": "assistant_text", "content": "indexing sources", "timestamp": 1700000010},
    ],
    _LIVE_INTERACTIVE: fixtures.MOCK_SESSION_ENTRIES,
}


def _build_fixture():
    return {
        "active_sessions": _ACTIVE_SESSIONS,
        "session_entries": _SESSION_ENTRIES,
        "recent_sessions": _RECENT_SESSIONS,
        "beads": [
            {"id": _DISP_BEAD, "title": "Fix X", "priority": 2, "status": "in_progress",
             "labels": ["readiness:approved"]},
        ],
        "runs": [
            {"id": f"{_DISP_BEAD}-0420-120000", "bead_id": _DISP_BEAD,
             "status": "RUNNING", "title": "Fix X", "priority": 2,
             "started_at": _iso(15), "completed_at": None,
             "duration_secs": 900, "run_dir": f"{_DISP_BEAD}-0420-120000"},
        ],
        "experiments": [fixtures.make_experiment(fixtures.TEST_EXPERIMENT_ID)],
    }


@pytest.fixture(scope="module")
def h(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("consolidation-ui")
    harness = SessionsTestHarness(tmp)
    harness.set_fixture(_build_fixture())
    harness.start_server()
    yield harness
    ab_raw("close")
    harness.stop()


# ── Test 12 — overlay updates live on a running dispatch ───────────────


class TestOverlayUpdatesLiveOnRunningDispatch:
    """#12 — Open /dispatch, click live trace on running bead, wait 10s,
    entry_count grows.

    FAILS TODAY: The dispatch overlay subscribes to the SSE channel for
    the run_dir, but the monitor doesn't broadcast for dispatch sessions
    (no register_session). So the overlay's entry count is frozen at the
    initial snapshot. Phase 2 wires dispatcher → monitor.register_session
    so session:messages fires for dispatch writes.
    """

    @pytest.mark.xfail(reason="investigating — see auto-hy3pl", strict=False)
    def test_overlay_updates_live_on_running_dispatch(self, h):
        ab_raw("close")
        ab_raw("open", f"http://localhost:{TEST_PORT}/dispatch",
               "--ignore-https-errors")
        time.sleep(3)

        # Trigger overlay via window._livePanelLoad — the dispatch page wires
        # this up automatically when clicking Live Trace; call directly to avoid
        # brittle CSS-selector clicks on a template we don't control.
        opened = ab_eval(f"""
            if (typeof window._livePanelLoad === 'function') {{
                window._livePanelLoad('{_DISP_BEAD}-0420-120000', true);
                return 'called';
            }}
            return 'missing';
        """)
        assert opened == "called", (
            f"window._livePanelLoad not registered; got {opened!r}. "
            "Overlay wiring is missing on /dispatch page."
        )

        # Read initial entry count from overlay state
        time.sleep(2)
        initial = ab_eval("""
            var panels = document.querySelectorAll('[x-data]');
            for (var i=0; i<panels.length; i++) {
                var d = panels[i]._x_dataStack && panels[i]._x_dataStack[0];
                if (d && Array.isArray(d.entries) && d.configure) {
                    return d.entries.length;
                }
            }
            return -1;
        """)
        assert isinstance(initial, (int, float)) and initial >= 0, (
            f"Could not read overlay entry count (initial={initial!r}). "
            "Overlay panel not rendering; live-panel wiring broken."
        )

        # Wait 10 seconds — on master, no SSE broadcasts fire for dispatch,
        # so count stays frozen. Phase 2 causes growth.
        time.sleep(10)

        final = ab_eval("""
            var panels = document.querySelectorAll('[x-data]');
            for (var i=0; i<panels.length; i++) {
                var d = panels[i]._x_dataStack && panels[i]._x_dataStack[0];
                if (d && Array.isArray(d.entries) && d.configure) {
                    return d.entries.length;
                }
            }
            return -1;
        """)

        assert isinstance(final, (int, float)) and final > initial, (
            f"Overlay entry count did not grow over 10s "
            f"(initial={initial}, final={final}). Monitor is not broadcasting "
            "session:messages for dispatch sessions — Phase 2 not yet wired."
        )


# ── Test 13 — Recent dispatch card navigates to a working viewer ──────


class TestRecentDispatchCardClickResolvesViewer:
    """#13 — Click Recent dispatch card → viewer shows label + entries.

    FAILS TODAY: sessions.js:502 uses a fallback chain
    (r.tmux_session || r.session_uuid || r.id). When the Recent row
    routes through tmux_session the viewer's configure() can't resolve
    the dispatch JSONL (tail endpoint returns 400), so the viewer shows
    an error or an empty state. Phase 3 + Phase 4 fix resolution.
    """

    def test_recent_dispatch_card_click_resolves_viewer(self, h):
        ab_raw("close")
        ab_raw("open", f"http://localhost:{TEST_PORT}/sessions",
               "--ignore-https-errors")
        time.sleep(3)

        # Click the Recent dispatch card by its row data-testid
        clicked = ab_eval("""
            var rows = document.querySelectorAll('[data-testid="recent-session-row"]');
            for (var i = 0; i < rows.length; i++) {
                if ((rows[i].textContent || '').indexOf('Fix X') !== -1) {
                    rows[i].click();
                    return 'clicked';
                }
            }
            return 'not_found';
        """)
        assert clicked == "clicked", (
            f"Recent dispatch row not found on /sessions; got {clicked!r}. "
            "Mock fixture not rendering or Recent-loop broken."
        )
        time.sleep(3)

        url = ab_eval("return window.location.pathname")
        assert isinstance(url, str) and "/session/" in url, (
            f"After Recent-card click, URL did not navigate to /session/...; "
            f"got {url!r}."
        )

        # Viewer must render with label as title + entries visible
        state = ab_eval("""
            var v = document.querySelector('.session-viewer');
            if (!v) return {error: 'no_viewer'};
            var cmp = typeof Alpine !== 'undefined' ? Alpine.$data(v) : null;
            if (!cmp) return {error: 'no_alpine'};
            return {
                state: cmp.state,
                label: cmp._label || '',
                entries: Array.isArray(cmp.entries) ? cmp.entries.length : -1,
                errorMsg: cmp.errorMsg || ''
            };
        """)
        assert isinstance(state, dict), f"Could not inspect viewer: {state!r}"
        assert state.get("state") == "ready", (
            f"Viewer state={state.get('state')!r}, errorMsg={state.get('errorMsg')!r}. "
            "Expected 'ready' — Recent-card dispatch click resolves to a broken "
            "viewer on master because tail endpoint can't resolve dispatch UUID."
        )
        assert state.get("entries", 0) >= 1, (
            f"Viewer has {state.get('entries')} entries; expected ≥1. "
            "Tail endpoint returned no entries for dispatch session."
        )


# ── Test 14 — Recent librarian card navigates to viewer ───────────────


class TestRecentLibrarianCardClickResolvesViewer:
    """#14 — Click Recent librarian card → viewer shows type+target + entries.

    FAILS TODAY: same resolution failure as #13 for librarian-type rows.
    Additionally, the Recent-card label formatting for librarian rows is
    handled in sessions.js via _librarianTitle; the viewer should show
    the derived label (e.g. "review_report · Target Bead") rather than
    the raw process name.
    """

    def test_recent_librarian_card_click_resolves_viewer(self, h):
        ab_raw("close")
        ab_raw("open", f"http://localhost:{TEST_PORT}/sessions",
               "--ignore-https-errors")
        time.sleep(3)

        clicked = ab_eval("""
            var rows = document.querySelectorAll('[data-testid="recent-session-row"]');
            for (var i = 0; i < rows.length; i++) {
                var txt = rows[i].textContent || '';
                if (txt.indexOf('review_report') !== -1 || txt.indexOf('librarian') !== -1) {
                    rows[i].click();
                    return 'clicked';
                }
            }
            return 'not_found';
        """)
        assert clicked == "clicked", (
            f"Recent librarian row not found; got {clicked!r}."
        )
        time.sleep(3)

        state = ab_eval("""
            var v = document.querySelector('.session-viewer');
            if (!v) return {error: 'no_viewer'};
            var cmp = typeof Alpine !== 'undefined' ? Alpine.$data(v) : null;
            if (!cmp) return {error: 'no_alpine'};
            return {
                state: cmp.state,
                label: cmp._label || '',
                entries: Array.isArray(cmp.entries) ? cmp.entries.length : -1,
                errorMsg: cmp.errorMsg || ''
            };
        """)
        assert isinstance(state, dict), f"Could not inspect viewer: {state!r}"
        assert state.get("state") == "ready", (
            f"Viewer state={state.get('state')!r}, errorMsg={state.get('errorMsg')!r}. "
            "Librarian Recent-card click lands on a broken viewer today."
        )
        assert state.get("entries", 0) >= 1, (
            f"Viewer has {state.get('entries')} entries; expected ≥1."
        )


# ── Test 15 — no phantom Active entry after Recent click + back-nav ───


class TestNoPhantomAfterRecentClick:
    """#15 — Click Recent dispatch card, history.back(), Active list must
    NOT contain that session_id.

    FAILS TODAY: session-viewer.js configure() creates an Alpine store
    entry keyed by the clicked session_id with default isLive=true. After
    back-navigation to /sessions, the Active list filters by
    store[id].isLive === true and includes that stale entry. Two bugs
    conspire: the isLive:true default (session-store.js:60) and the
    fallback session id chain in sessions.js (line 502).
    """

    def test_no_phantom_after_recent_click_back_nav(self, h):
        ab_raw("close")
        ab_raw("open", f"http://localhost:{TEST_PORT}/sessions",
               "--ignore-https-errors")
        time.sleep(3)

        # Click the dispatch Recent row
        ab_eval("""
            var rows = document.querySelectorAll('[data-testid="recent-session-row"]');
            for (var i = 0; i < rows.length; i++) {
                if ((rows[i].textContent || '').indexOf('Fix X') !== -1) {
                    rows[i].click();
                    return 'clicked';
                }
            }
            return 'not_found';
        """)
        time.sleep(2)

        # Navigate back
        ab_eval("window.history.back(); return 'back';")
        time.sleep(3)

        # Active list must NOT contain the dispatch tmux name or session_uuid
        active_ids = ab_eval("""
            var cards = document.querySelectorAll('[data-testid="session-card"]');
            return Array.from(cards).map(function(c) { return c.dataset.sessionId; });
        """) or []
        assert isinstance(active_ids, list)
        assert _DISPATCH_TMUX not in active_ids, (
            f"Active list includes phantom dispatch tmux {_DISPATCH_TMUX!r}: "
            f"{active_ids}. Back-navigation from Recent-card viewer leaves a "
            "live Alpine store entry that sessions.js renders as an Active card."
        )
        assert _DISPATCH_UUID not in active_ids, (
            f"Active list contains phantom dispatch UUID {_DISPATCH_UUID!r}: "
            f"{active_ids}."
        )


# ── Test 20 — dispatch session absent from Active list ────────────────


class TestDispatchNotInActiveList:
    """#20 — GET /api/dao/active_sessions while dispatch running: dispatch
    session_uuid absent.

    FAILS TODAY: get_active_sessions() returns all is_live=1 rows
    regardless of type. Phase 5 adds a type-filter
    (`type IN ('container','host')`) so dispatch + librarian are
    first-class in the registry but hidden from the Active list.
    """

    def test_dispatch_not_in_active_list(self, h):
        import httpx
        resp = httpx.get(
            f"http://localhost:{TEST_PORT}/api/dao/active_sessions",
            timeout=5,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

        # Indexing both possible id keys for the dispatch row
        for session in data:
            sid = session.get("session_id") or session.get("tmux_session") or ""
            stype = session.get("type", "")
            assert stype != "dispatch", (
                f"Dispatch session {sid!r} found on Active list with "
                f"type='dispatch'. Phase 5 filter not yet in place: "
                "get_active_sessions must exclude type IN "
                "('dispatch','librarian')."
            )
            assert sid != _DISPATCH_TMUX, (
                f"Dispatch tmux {_DISPATCH_TMUX!r} leaked into Active list. "
                f"row={session!r}"
            )


# ── Test 21 — librarian session absent from Active list ───────────────


class TestLibrarianNotInActiveList:
    """#21 — Same as #20 for librarian.

    FAILS TODAY: identical reason — get_active_sessions doesn't filter by
    type. Phase 5 fixes.
    """

    def test_librarian_not_in_active_list(self, h):
        import httpx
        resp = httpx.get(
            f"http://localhost:{TEST_PORT}/api/dao/active_sessions",
            timeout=5,
        )
        assert resp.status_code == 200
        data = resp.json()

        for session in data:
            sid = session.get("session_id") or session.get("tmux_session") or ""
            stype = session.get("type", "")
            assert stype != "librarian", (
                f"Librarian session {sid!r} on Active list. "
                "Phase 5 filter not yet in place."
            )
            assert sid != _LIB_TMUX, (
                f"Librarian tmux {_LIB_TMUX!r} leaked into Active list. "
                f"row={session!r}"
            )
