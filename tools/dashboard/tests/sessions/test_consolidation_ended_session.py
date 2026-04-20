"""
Phase 0 — Ended-dispatch viewer rendering: L2 browser tests.

Bead auto-ylj6r. Tests the session viewer (/session/{project}/{id})
against ended/dead dispatch sessions. All MUST FAIL on master.

Covered tests from the bead's Phase 0 test table:
  #16 test_viewer_renders_ended_dispatch_session
  #17 test_ended_dispatch_shows_no_input_bar
  #18 test_ended_dispatch_shows_static_title_and_entry_count
  #19 test_viewer_error_state_on_missing_session

The ended-dispatch UI contract is:
  - sv-input NOT rendered
  - no .contenteditable title
  - "N entries" text shown somewhere in the viewer

Uses the mock-fixture harness (DASHBOARD_MOCK) + agent-browser.
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


_ENDED_DISPATCH_UUID = "ccccdddd-eeee-ffff-0000-111111111111"
_ENDED_DISPATCH_TMUX = "auto-ended-0420-130000"


def _build_fixture():
    ended_session = {
        **make_session(
            _ENDED_DISPATCH_TMUX, label="Ended dispatch — Fix X",
            role="dispatch", last_message="done",
            entry_count=42, context_tokens=50000,
        ),
        "type": "dispatch",
        "is_live": False,
        "session_uuid": _ENDED_DISPATCH_UUID,
        # session_id is what mock get_active_sessions keys by
        "session_id": _ENDED_DISPATCH_TMUX,
    }
    entries = [
        {"type": "user", "content": "fix X please", "timestamp": 1700000000},
        {"type": "assistant_text", "content": "Looking at the code", "timestamp": 1700000005},
        {"type": "tool_use", "tool_id": "t1", "tool_name": "Read",
         "content": "Read foo.py", "timestamp": 1700000010},
        {"type": "tool_result", "tool_id": "t1", "content": "...",
         "timestamp": 1700000015},
        {"type": "assistant_text", "content": "done — all tests pass",
         "timestamp": 1700000020},
    ]
    return {
        "active_sessions": [ended_session],
        "session_entries": {
            _ENDED_DISPATCH_TMUX: entries,
            _ENDED_DISPATCH_UUID: entries,
        },
        "recent_sessions": [{
            "id": "src-ended-001",
            "type": "session",
            "title": "Ended dispatch — Fix X",
            "project": "autonomy",
            "session_uuid": _ENDED_DISPATCH_UUID,
            "tmux_session": _ENDED_DISPATCH_TMUX,
            "session_type": "dispatch",
            "entry_count": 42,
            "context_tokens": 50000,
            "last_activity_at": _iso(60),
            "created_at": _iso(180),
            "ended_at": _iso(60),
        }],
        "beads": [],
        "experiments": [fixtures.make_experiment(fixtures.TEST_EXPERIMENT_ID)],
    }


@pytest.fixture(scope="module")
def h(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("consolidation-ended")
    harness = SessionsTestHarness(tmp)
    harness.set_fixture(_build_fixture())
    harness.start_server()
    yield harness
    ab_raw("close")
    harness.stop()


def _open_viewer(project: str, session_id: str) -> None:
    ab_raw("close")
    ab_raw("open",
           f"http://localhost:{TEST_PORT}/session/{project}/{session_id}",
           "--ignore-https-errors")
    time.sleep(3)


# ── Test 16 — viewer renders ended dispatch by UUID ───────────────────


class TestViewerRendersEndedDispatch:
    """#16 — Open /session/{proj}/{ended_dispatch_uuid} → viewer renders
    with entries.

    FAILS TODAY: api_session_tail does not resolve by session_uuid
    (tmux_sessions.tmux_name PRIMARY KEY lookup only). The viewer's
    configure({sessionId}) path calls /api/session/{proj}/{uuid}/tail,
    receives 400, and enters 'error' state. Phase 3 adds UUID resolution.
    """

    def test_viewer_renders_ended_dispatch_session(self, h):
        _open_viewer("autonomy", _ENDED_DISPATCH_UUID)
        time.sleep(2)

        state = ab_eval("""
            var v = document.querySelector('.session-viewer');
            if (!v) return {error: 'no_viewer_root'};
            var cmp = typeof Alpine !== 'undefined' ? Alpine.$data(v) : null;
            if (!cmp) return {error: 'no_alpine_cmp'};
            return {
                state: cmp.state,
                isLive: cmp.isLive,
                entries: Array.isArray(cmp.entries) ? cmp.entries.length : -1,
                errorMsg: cmp.errorMsg || ''
            };
        """)
        assert isinstance(state, dict), f"viewer introspection failed: {state!r}"
        assert state.get("state") == "ready", (
            f"Expected viewer.state='ready', got {state.get('state')!r} "
            f"(errorMsg={state.get('errorMsg')!r}). The tail endpoint "
            "cannot resolve ended dispatch UUIDs today."
        )
        assert state.get("entries", 0) >= 1, (
            f"Expected ≥1 entry from ended dispatch; got {state.get('entries')}"
        )


# ── Test 17 — no input bar on ended dispatch ──────────────────────────


class TestEndedDispatchNoInputBar:
    """#17 — Ended dispatch viewer: no .sv-input template rendered.

    FAILS TODAY: Even if the viewer loads the ended dispatch, the sv-input
    template gates only on _tmuxSession presence (see session-view.html
    around line 183). For ended sessions that land in the viewer with
    _tmuxSession still populated but is_live=false, an input bar still
    renders. Phase 4 tightens the gate: sv-input only when isLive===true.
    """

    @pytest.mark.xfail(reason="investigating — see auto-hy3pl", strict=False)
    def test_ended_dispatch_shows_no_input_bar(self, h):
        _open_viewer("autonomy", _ENDED_DISPATCH_UUID)
        time.sleep(2)

        input_rendered = ab_eval("""
            // Count visible sv-input elements
            var inputs = document.querySelectorAll('.sv-input');
            var visible = 0;
            for (var i = 0; i < inputs.length; i++) {
                var cs = getComputedStyle(inputs[i]);
                if (cs.display !== 'none' && cs.visibility !== 'hidden') {
                    visible++;
                }
            }
            return visible;
        """)
        assert input_rendered == 0, (
            f"Ended dispatch viewer rendered {input_rendered} .sv-input element(s); "
            "expected 0. Input bar must be hidden for dead sessions."
        )


# ── Test 18 — static title and entry count on ended dispatch ──────────


class TestEndedDispatchStaticTitle:
    """#18 — Dead dispatch viewer: static title (no contenteditable) and
    "N entries" text shown.

    FAILS TODAY: the viewer header template branches on _tmuxSession +
    isLive to pick the title element, and none of the current branches
    render a contenteditable-free static title for dead dispatch
    sessions. "N entries" text is conditional on !isLive in
    session-view.html:47 but requires the viewer to reach 'ready' state
    first, which is blocked by Phase 3 (tail resolution).
    """

    @pytest.mark.xfail(reason="investigating — see auto-hy3pl", strict=False)
    def test_ended_dispatch_shows_static_title_and_entry_count(self, h):
        _open_viewer("autonomy", _ENDED_DISPATCH_UUID)
        time.sleep(2)

        result = ab_eval("""
            var v = document.querySelector('.session-viewer');
            if (!v) return {error: 'no_viewer_root'};
            var cmp = typeof Alpine !== 'undefined' ? Alpine.$data(v) : null;
            if (!cmp || cmp.state !== 'ready') {
                return {error: 'viewer_not_ready', state: cmp && cmp.state};
            }
            var title = document.querySelector('.session-viewer .session-title');
            var hasContentEditable = false;
            if (title) {
                hasContentEditable = title.hasAttribute('contenteditable') &&
                                     title.getAttribute('contenteditable') !== 'false';
            }
            var body = document.body.innerText || '';
            return {
                hasTitle: !!title,
                hasContentEditable: hasContentEditable,
                entriesText: /\\d+\\s+entries?/i.test(body),
                body: body.substring(0, 500)
            };
        """)
        assert isinstance(result, dict), f"Could not inspect viewer: {result!r}"
        if result.get("error"):
            pytest.fail(
                f"Viewer never entered ready state: {result!r}. "
                "Blocked by Phase 3 (tail UUID resolution)."
            )
        assert result.get("hasTitle") is True, (
            "No .session-title element rendered on ended dispatch viewer. "
            "Dead sessions need a static title surfaced in the header."
        )
        assert result.get("hasContentEditable") is False, (
            "Title is contenteditable on a dead session. "
            "Phase 4 requires the dead-dispatch path to render a non-editable title."
        )
        assert result.get("entriesText") is True, (
            f"'N entries' text missing from dead dispatch viewer. "
            f"body excerpt={result.get('body')!r}"
        )


# ── Test 19 — viewer error state for missing session ──────────────────


class TestViewerErrorStateOnMissingSession:
    """#19 — /session/{proj}/{nonexistent_id} → viewer renders error state.

    FAILS TODAY: the viewer crashes or hangs in 'loading' state when the
    tail endpoint returns 400 with an "Invalid project or session_id"
    body. Phase 3 returns 404 with a structured error; Phase 4 teaches
    session-viewer.js to catch 404 and transition to state='error' with
    an explanatory errorMsg.
    """

    def test_viewer_error_state_on_missing_session(self, h):
        _open_viewer("autonomy", "ghost-99999999-0000-0000-0000-000000000000")
        time.sleep(3)

        state = ab_eval("""
            var v = document.querySelector('.session-viewer');
            if (!v) return {error: 'no_viewer_root'};
            var cmp = typeof Alpine !== 'undefined' ? Alpine.$data(v) : null;
            if (!cmp) return {error: 'no_alpine_cmp'};
            return {
                state: cmp.state,
                errorMsg: cmp.errorMsg || ''
            };
        """)
        assert isinstance(state, dict), f"Could not inspect viewer: {state!r}"
        assert state.get("state") == "error", (
            f"Expected viewer.state='error' for missing session; "
            f"got {state.get('state')!r} (errorMsg={state.get('errorMsg')!r}). "
            "Master hangs in 'loading' or crashes on unknown session id."
        )
        assert isinstance(state.get("errorMsg"), str) and state["errorMsg"], (
            f"Error state must include a non-empty errorMsg; got {state!r}"
        )
