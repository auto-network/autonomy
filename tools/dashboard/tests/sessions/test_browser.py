"""
Browser functional tests for the sessions page.

Tests BEHAVIOR through the user's perspective — not CSS classes or DOM structure.
Uses DASHBOARD_MOCK fixtures for data, agent-browser for interaction.

Every test answers: "Can the user see X?" — not "Does CSS class Y exist?"
"""
import json
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from tools.dashboard.tests import fixtures
from tools.dashboard.tests._xdist import bind_free_port, worker_test_port
from datetime import datetime, timedelta, timezone

from tools.dashboard.tests.sessions.conftest import (
    SESSIONS_PAGE_SESSIONS,
    RECENT_SESSIONS,
    sessions_page_fixture,
)


def _iso_inline(minutes_ago: int = 0) -> str:
    """Produce an ISO timestamp `minutes_ago` minutes before now."""
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


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

class SessionsTestHarness:
    """Manages test server + fixture + browser for sessions page tests."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.fixture_path = tmp_path / "fixtures.json"
        self.events_file = tmp_path / "events.jsonl"
        # Touch so the mock watcher's .exists() poll finds it immediately
        self.events_file.touch()
        self.proc = None
        self.nonce = uuid.uuid4().hex

    def set_fixture(self, fixture_dict):
        """Swap the fixture data. Mock DAO reads fresh on every request."""
        fixtures.write_fixture(
            {**fixture_dict, "__harness_nonce__": self.nonce}, self.fixture_path
        )

    def start_server(self):
        # Bind a kernel-assigned free port and hand its descriptor to uvicorn
        # via --fd. worker_test_port is worker-index-derived with no session
        # dimension, so two sessions bind the same port and a readiness probe
        # can answer from the other session's server (port-collision report,
        # auto-0812-211339). An OS-assigned port we already hold cannot collide.
        global TEST_PORT
        sock, TEST_PORT = bind_free_port()

        env = os.environ.copy()
        env["DASHBOARD_MOCK"] = str(self.fixture_path)
        env["DASHBOARD_MOCK_EVENTS"] = str(self.events_file)
        repo_root = str(Path(__file__).resolve().parents[4])
        env["PYTHONPATH"] = repo_root
        self.proc = subprocess.Popen(
            ["python3", "-m", "uvicorn", "tools.dashboard.server:app",
             "--fd", str(sock.fileno())],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=repo_root, pass_fds=(sock.fileno(),),
        )
        sock.close()
        import httpx
        # 30s window: uvicorn imports the full server module; under 8-way
        # xdist contention plus per-module Chromium cold boots, 10s is
        # routinely exceeded on a loaded machine.
        for _ in range(60):
            try:
                r = httpx.get(
                    f"http://localhost:{TEST_PORT}/api/_mock/harness-nonce",
                    timeout=1,
                )
                if r.status_code == 200:
                    if r.json().get("nonce") != self.nonce:
                        self.stop()
                        raise RuntimeError(
                            "Mock server identity check failed: reached a "
                            "server that is not ours (nonce mismatch)."
                        )
                    return
            except httpx.HTTPError:
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

    def open_sessions_page(self):
        ab_raw("close")
        ab_raw("open", f"http://localhost:{TEST_PORT}/sessions",
               "--ignore-https-errors")
        # A cold Chromium under parallel load routinely blows past the old
        # fixed 3s sleep — poll until the page has actually painted either
        # session cards or its explicit empty state.
        deadline = time.time() + 20
        while time.time() < deadline:
            painted = ab_eval("""
                if (!window.Alpine) return false;
                if (document.querySelectorAll('[data-testid="session-card"]').length > 0) return true;
                return (document.body.innerText || '').indexOf('No ') !== -1;
            """)
            if painted is True:
                break
            time.sleep(0.5)
        ab_raw("set", "viewport", "430", "900")
        time.sleep(0.5)

    def card_count(self):
        """Count rendered session cards."""
        return ab_eval("""
            var cards = document.querySelectorAll('[data-testid="session-card"]');
            return cards.length;
        """)

    def card_session_ids(self):
        """Get data-session-id values from all cards."""
        return ab_eval("""
            var cards = document.querySelectorAll('[data-testid="session-card"]');
            return Array.from(cards).map(function(c) { return c.dataset.sessionId; });
        """) or []

    def visible_text(self):
        return ab_eval("return document.body.innerText") or ""

    def store_topics(self, session_id):
        """Read topics from Alpine.store('sessions')[session_id].topics."""
        return ab_eval(f"""
            var sessions = Alpine.store('sessions');
            var s = sessions && sessions['{session_id}'];
            if (s) return s.topics;
            return null;
        """)

    def store_nag(self, session_id):
        """Read nag fields from Alpine store."""
        return ab_eval(f"""
            var sessions = Alpine.store('sessions');
            var s = sessions && sessions['{session_id}'];
            if (s) return {{
                nagEnabled: s.nagEnabled,
                nagInterval: s.nagInterval,
                nagMessage: s.nagMessage
            }};
            return null;
        """)

    def topic_texts(self):
        """Get visible topic text from all sc-topic-item elements."""
        return ab_eval("""
            var items = document.querySelectorAll('.sc-topic-item');
            return Array.from(items).map(function(el) { return el.textContent.trim(); });
        """) or []

    def nag_bells_visible(self):
        """Count visible (active) nag bell indicators."""
        return ab_eval("""
            var bells = document.querySelectorAll('.sc-nag:not(.sc-nag-off)');
            return bells.length;
        """)

    def stats_text(self):
        """Get text content of stats row elements."""
        return ab_eval("""
            var vals = document.querySelectorAll('.sc-t3-val');
            return Array.from(vals).map(function(el) { return el.textContent.trim(); });
        """) or []


# ── Module-scoped fixture ────────────────────────────────────────────

@pytest.fixture(scope="module")
def h(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sessions")
    harness = SessionsTestHarness(tmp)
    harness.set_fixture(sessions_page_fixture())
    harness.start_server()
    try:
        # try/finally so a setup failure still stops the uvicorn server —
        # a leaked server poisons the next file on this xdist worker.
        harness.open_sessions_page()
        yield harness
    finally:
        ab_raw("close")
        harness.stop()


# ── Tests ────────────────────────────────────────────────────────────


class TestUserCanSeeSessions:
    """When I open /sessions, can I see my active sessions?"""

    def test_cards_render(self, h):
        count = h.card_count()
        assert count >= 5, f"expected 5+ session cards, got {count}"

    def test_session_ids_set(self, h):
        ids = h.card_session_ids()
        assert "auto-test-alpha" in ids
        assert "host-test-delta" in ids

    def test_labels_visible(self, h):
        text = h.visible_text()
        assert "Alpha" in text
        assert "Beta Builder" in text

    def test_host_badge(self, h):
        text = h.visible_text()
        assert "Host" in text

    def test_role_badges(self, h):
        text = h.visible_text()
        assert "Designer" in text
        assert "Builder" in text


class TestSessionOverlayNavigation:
    """Opening a session from the list should preserve the mounted list underneath."""

    def test_open_and_back_preserve_sessions_dom(self, h):
        ab_eval("""
            var card = document.querySelector('[data-testid="session-card"][data-session-id="auto-test-alpha"]');
            if (card) card.dataset.overlayMarker = 'keep';
            navigateTo('/session/autonomy/auto-test-alpha?tmux=auto-test-alpha');
            return true;
        """)
        time.sleep(1.5)

        overlay_state = ab_eval("""
            var layer = document.getElementById('session-view-layer');
            var host = document.getElementById('session-view-host');
            var stack = document.getElementById('page-stack');
            var marked = document.querySelector('#content [data-overlay-marker="keep"]');
            return {
              path: window.location.pathname,
              overlayActive: !!(layer && layer.classList.contains('active')),
              fullscreen: document.body.classList.contains('fullscreen-page'),
              overlayBodyClass: document.body.classList.contains('session-overlay-active'),
              overlayPosition: layer ? getComputedStyle(layer).position : null,
              stackDisplay: stack ? getComputedStyle(stack).display : null,
              markedCardStillMounted: !!marked,
              overlayHostChildren: host ? host.children.length : -1,
            };
        """)
        assert overlay_state["path"] == "/session/autonomy/auto-test-alpha"
        assert overlay_state["overlayActive"], "Session overlay should be active when opened from /sessions"
        assert not overlay_state["fullscreen"], "Overlay navigation should not toggle body.fullscreen-page"
        assert overlay_state["overlayBodyClass"], "Overlay navigation should toggle the stacked mobile overlay body class"
        assert overlay_state["overlayPosition"] != "fixed", "Session overlay layer must not use position:fixed on mobile"
        assert overlay_state["stackDisplay"] == "grid", "Page stack should keep the mounted sessions list and overlay in one grid shell"
        assert overlay_state["markedCardStillMounted"], "Sessions list should remain mounted underneath the overlay"
        assert overlay_state["overlayHostChildren"] >= 1, "Overlay host should contain the mounted session viewer"

        ab_eval("""
            history.back();
            return true;
        """)
        time.sleep(1.0)

        back_state = ab_eval("""
            var layer = document.getElementById('session-view-layer');
            var host = document.getElementById('session-view-host');
            var marked = document.querySelector('#content [data-overlay-marker="keep"]');
            return {
              path: window.location.pathname,
              overlayActive: !!(layer && layer.classList.contains('active')),
              overlayBodyClass: document.body.classList.contains('session-overlay-active'),
              markedCardStillMounted: !!marked,
              overlayHostChildren: host ? host.children.length : -1,
            };
        """)
        assert back_state["path"] == "/sessions"
        assert not back_state["overlayActive"], "Overlay should close on Back"
        assert not back_state["overlayBodyClass"], "Closing the overlay should clear the mobile overlay body class"
        assert back_state["markedCardStillMounted"], "Back should reveal the original sessions DOM instead of rebuilding it"
        # 25c8ec7 fast-paths overlay Back: the viewer stays mounted in the
        # (hidden) host so an immediate re-open is instant. Only a real
        # route() away from /sessions destroys it.
        assert back_state["overlayHostChildren"] >= 1, (
            "Fast-path Back should leave the session viewer mounted in the hidden overlay host"
        )

    def test_desktop_navigation_keeps_dashboard_chrome_visible(self, h):
        ab_raw("set", "viewport", "1280", "900")
        time.sleep(0.5)
        try:
            ab_eval("""
                navigateTo('/session/autonomy/auto-test-alpha?tmux=auto-test-alpha');
                return true;
            """)
            time.sleep(1.5)

            desktop_state = ab_eval("""
                var layer = document.getElementById('session-view-layer');
                var content = document.querySelector('main#content');
                var viewer = document.querySelector('main#content .session-viewer');
                var header = document.querySelector('main#content .sv-header');
                var input = document.querySelector('main#content .sv-input');
                var entries = document.querySelector('main#content .sv-entries');
                var contentRect = content ? content.getBoundingClientRect() : null;
                var headerRect = header ? header.getBoundingClientRect() : null;
                var inputRect = input ? input.getBoundingClientRect() : null;
                var entriesRect = entries ? entries.getBoundingClientRect() : null;
                return {
                  path: window.location.pathname,
                  overlayActive: !!(layer && layer.classList.contains('active')),
                  fullscreen: document.body.classList.contains('fullscreen-page'),
                  headerVisible: (function(){ var e=document.querySelector('header'); return !!(e && e.offsetParent !== null); })(),
                  sidebarVisible: (function(){ var e=document.getElementById('sidebar'); return !!(e && e.offsetParent !== null); })(),
                  viewerInContent: !!viewer,
                  inputVisible: !!(input && input.offsetParent !== null),
                  inputInViewport: !!(inputRect && contentRect && inputRect.top >= contentRect.top && inputRect.bottom <= contentRect.bottom),
                  entriesBounded: !!(
                    entriesRect &&
                    headerRect &&
                    inputRect &&
                    entriesRect.top >= (headerRect.bottom - 1) &&
                    entriesRect.bottom <= (inputRect.top + 1)
                  ),
                };
            """)
            assert desktop_state["path"] == "/session/autonomy/auto-test-alpha"
            assert not desktop_state["overlayActive"], "Desktop session open should not use the fullscreen overlay"
            assert not desktop_state["fullscreen"], "Desktop session open should not toggle mobile fullscreen mode"
            assert desktop_state["headerVisible"], "Desktop session view should keep the global header visible"
            assert desktop_state["sidebarVisible"], "Desktop session view should keep the sidebar visible"
            assert desktop_state["viewerInContent"], "Desktop session view should render in the main content area"
            assert desktop_state["inputVisible"], "Desktop session view should keep the message composer visible"
            assert desktop_state["inputInViewport"], "Desktop session composer must remain inside the viewport"
            assert desktop_state["entriesBounded"], "Desktop transcript pane must stay bounded between the header and composer"
        finally:
            ab_eval("""
                if (window.location.pathname !== '/sessions') navigateTo('/sessions');
                return true;
            """)
            time.sleep(1.0)
            ab_raw("set", "viewport", "430", "900")
            time.sleep(0.5)

    def test_mobile_session_overlay_worktree_review_stacks_above_viewer(self, h):
        ab_eval("""
            navigateTo('/session/autonomy/auto-test-alpha?tmux=auto-test-alpha');
            return true;
        """)
        time.sleep(1.5)
        try:
            open_state = ab_eval("""
                var reviewBtn = document.querySelector('#session-view-layer [data-testid="session-worktree-review-button"]');
                if (reviewBtn) reviewBtn.click();
                return !!reviewBtn;
            """)
            assert open_state, "Mobile session overlay did not render the worktree review button"
            time.sleep(1.0)

            review_state = ab_eval("""
                var detail = document.querySelector('[data-testid="worktree-commit-detail"]')
                  || document.querySelector('[data-testid="worktree-dirty-detail"]');
                var shell = detail ? detail.querySelector('.worktree-review-shell') : null;
                var pointEl = document.elementFromPoint(Math.floor(window.innerWidth / 2), Math.min(window.innerHeight - 20, 120));
                var topDetail = pointEl && pointEl.closest('[data-testid="worktree-commit-detail"], [data-testid="worktree-dirty-detail"]');
                var wtZ = detail ? getComputedStyle(detail).zIndex : '';
                var svLayer = document.getElementById('session-view-layer');
                var svZ = svLayer ? getComputedStyle(svLayer).zIndex : '';
                return {
                  path: window.location.pathname,
                  detailOpen: !!detail,
                  shellVisible: !!(shell && shell.offsetParent !== null),
                  topmostIsReview: !!topDetail,
                  worktreeOverlayZ: wtZ,
                  sessionLayerZ: svZ,
                };
            """)
            assert review_state["path"] == "/session/autonomy/auto-test-alpha"
            assert review_state["detailOpen"], "Worktree review did not open from the mobile session overlay"
            assert review_state["shellVisible"], "Worktree review shell is not visible after opening"
            assert review_state["topmostIsReview"], "Worktree review should stack above the fullscreen session viewer on mobile"
            assert int(review_state["worktreeOverlayZ"] or 0) > int(review_state["sessionLayerZ"] or 0), (
                "Worktree review overlay z-index must exceed the fullscreen session-view layer"
            )
        finally:
            ab_eval("""
                if (window._worktreeReviewOverlay) {
                  window._worktreeReviewOverlay.selectedCommit = null;
                  window._worktreeReviewOverlay.selectedDirtyRow = null;
                }
                if (window.location.pathname !== '/sessions') history.back();
                return true;
            """)
            time.sleep(1.0)


class TestTopicsRender:
    """Topics from the API appear on session cards."""

    def test_store_has_topics(self, h):
        """Acceptance test: store.topics is populated (not empty default)."""
        topics = h.store_topics("auto-test-alpha")
        assert topics is not None
        assert len(topics) >= 2, f"expected 2+ topics, got {topics}"

    def test_topics_visible_on_cards(self, h):
        topics = h.topic_texts()
        assert len(topics) >= 3, f"expected 3+ topic items visible, got {topics}"

    def test_topic_text_matches_fixtures(self, h):
        topics = h.topic_texts()
        assert "Redesigning session cards" in topics
        assert "Asset pipeline" in topics


class TestNagIndicator:
    """Sessions with active nag show a bell indicator."""

    def test_active_nag_bell_visible(self, h):
        # Gamma has nag_enabled — at least 1 visible (non-off) bell per card
        # Cards have both compact and stats row bells, so count may vary
        count = h.nag_bells_visible()
        assert count >= 1, f"expected at least 1 active nag bell, got {count}"


class TestLiveCardActionsMenu:
    """Live cards expose Restart immediately above destructive Close."""

    def test_restart_is_immediately_above_close(self, h):
        labels = ab_eval("""
            var card = document.querySelector('[data-session-id="auto-test-alpha"]');
            var btn = card && card.querySelector('[data-testid="session-actions-btn"]');
            if (!btn) return null;
            btn.click();
            var store = Alpine.store('actionSheet');
            return store.actions.map(function(action) { return action.label; });
        """)
        try:
            assert labels is not None, "Live session actions button was not found"
            restart = labels.index("Restart Session")
            close = labels.index("Close Session")
            assert close == restart + 1, f"Unexpected live action order: {labels}"
        finally:
            ab_eval("window.actionSheet.dismiss(); return true;")


class TestStatsRow:
    """Turn counts and context tokens appear in stats."""

    def test_turns_visible(self, h):
        text = h.visible_text()
        assert "150" in text or "200" in text or "300" in text

    def test_context_tokens_visible(self, h):
        # 80000 → "80K", 120000 → "120K", 250000 → "250K"
        text = h.visible_text()
        assert "80K" in text or "120K" in text or "250K" in text


class TestStableActiveOrdering:
    """Active cards move only at an explicit ordering boundary."""

    def test_launching_card_enters_active_at_top_and_stays_while_visible(self, h):
        transition = ab_eval("""
            var root = document.querySelector('[x-data="sessionsPage()"]');
            var d = root && root._x_dataStack && root._x_dataStack[0];
            if (!d) return null;
            window.__launchOrderTest = {
              interactive: d.interactive,
              activeOrder: d._activeOrder,
              launchingIds: d._launchingSessionIds,
              activeSortDirection: d.activeSortDirection,
            };
            d.activeSortDirection = 'desc';
            var launched = {
              session_id: 'auto-just-launched',
              type: 'container',
              created_at: Date.now() / 1000 + 100,
              last_activity: 0,
              last_input_at: 0,
              entry_count: -1,
              context_tokens: -1,
              _launching: true,
            };
            d.interactive = d.interactive.concat([launched]);
            d.refreshActiveOrder();
            var whileLaunching = d.sortedInteractive.map(function(s) { return s.session_id; });
            launched._launching = false;
            d._reconcileActiveOrder();
            var afterLaunch = d.sortedInteractive.map(function(s) { return s.session_id; });
            d._reconcileActiveOrder();
            var whileVisible = d.sortedInteractive.map(function(s) { return s.session_id; });
            return {
              whileLaunching: whileLaunching,
              afterLaunch: afterLaunch,
              whileVisible: whileVisible,
            };
        """)
        try:
            assert transition is not None
            assert "auto-just-launched" not in transition["whileLaunching"]
            assert transition["afterLaunch"][0] == "auto-just-launched"
            assert transition["whileVisible"] == transition["afterLaunch"]
        finally:
            ab_eval("""
                var root = document.querySelector('[x-data="sessionsPage()"]');
                var d = root._x_dataStack[0];
                var saved = window.__launchOrderTest;
                d.interactive = saved.interactive;
                d._activeOrder = saved.activeOrder;
                d._launchingSessionIds = saved.launchingIds;
                d.activeSortDirection = saved.activeSortDirection;
                delete window.__launchOrderTest;
                return true;
            """)

    def test_live_activity_does_not_move_cards_until_foreground(self, h):
        before = ab_eval("""
            var root = document.querySelector('[x-data="sessionsPage()"]');
            var d = root && root._x_dataStack && root._x_dataStack[0];
            if (!d) return null;
            d.activeSort = 'lastActivity';
            d.activeSortDirection = 'desc';
            d.refreshActiveOrder();
            var cards = document.querySelectorAll(
              '[data-testid="active-sessions-section"] [data-testid="session-card"]'
            );
            var ids = Array.from(cards).map(function(c) { return c.dataset.sessionId; });
            var target = ids[ids.length - 1];
            window.__stableSortTest = {
              target: target,
              previous: Alpine.store('sessions')[target].lastActivity,
              before: ids,
            };
            Alpine.store('sessions')[target].lastActivity = Date.now() / 1000 + 10000;
            window.dispatchEvent(new CustomEvent('sessions:store-changed', {detail:{reason:'test'}}));
            return window.__stableSortTest;
        """)
        assert before and before["target"]
        time.sleep(0.5)
        while_visible = ab_eval("""
            return Array.from(document.querySelectorAll(
              '[data-testid="active-sessions-section"] [data-testid="session-card"]'
            )).map(function(c) { return c.dataset.sessionId; });
        """)
        assert while_visible == before["before"], (
            "A live activity update reordered cards while the list was visible"
        )

        after = ab_eval("""
            window.dispatchEvent(new CustomEvent('app:navigated', {detail:{path:'/sessions'}}));
            var root = document.querySelector('[x-data="sessionsPage()"]');
            return root._x_dataStack[0].sortedInteractive.map(function(s) { return s.session_id; });
        """)
        assert after[0] == before["target"], "Foregrounding Sessions did not apply the latest order"

        ab_eval("""
            var t = window.__stableSortTest;
            Alpine.store('sessions')[t.target].lastActivity = t.previous;
            var root = document.querySelector('[x-data="sessionsPage()"]');
            root._x_dataStack[0]._updateFromStore();
            root._x_dataStack[0].refreshActiveOrder();
            delete window.__stableSortTest;
            return true;
        """)

    def test_recent_input_and_direction_toggle(self, h):
        descending = ab_eval("""
            var root = document.querySelector('[x-data="sessionsPage()"]');
            var d = root && root._x_dataStack && root._x_dataStack[0];
            if (!d) return null;
            d.activeSort = 'recentInput';
            d.activeSortDirection = 'desc';
            d.refreshActiveOrder();
            return d.sortedInteractive.map(function(s) { return s.session_id; });
        """)
        assert descending[:4] == [
            "auto-test-beta", "host-test-delta", "auto-test-alpha", "auto-test-gamma"
        ]
        assert descending[-1] == "auto-test-epsilon", "A session with no input should be oldest"

        ascending = ab_eval("""
            var button = document.querySelector('[data-testid="active-sort-direction"]');
            button.click();
            var root = document.querySelector('[x-data="sessionsPage()"]');
            var d = root._x_dataStack[0];
            return {
              ids: d.sortedInteractive.map(function(s) { return s.session_id; }),
              label: d.activeSortDirectionTitle(),
            };
        """)
        assert ascending["ids"][0] == "auto-test-epsilon"
        assert "Ascending" in ascending["label"]

        ab_eval("""
            var root = document.querySelector('[x-data="sessionsPage()"]');
            var d = root._x_dataStack[0];
            d.activeSort = 'lastActivity';
            d.activeSortDirection = 'desc';
            localStorage.setItem('sessionsActiveSortDirection', 'desc');
            d._updateFromStore();
            d.refreshActiveOrder();
            return true;
        """)

    def test_recent_input_is_available_in_sort_menu(self, h):
        ab_eval("""
            document.querySelector('[data-testid="active-sort-toggle"]').click();
            return true;
        """)
        time.sleep(0.2)
        options = ab_eval("""
            var menu = document.querySelector('[data-testid="active-sort-toggle-menu"]');
            return Array.from(menu.querySelectorAll('.sort-option'))
              .map(function(o) { return o.textContent.trim(); });
        """)
        assert any("Recent Input" in option for option in options)
        ab_eval("""
            document.querySelector('[data-testid="active-sort-toggle"]').click();
            return true;
        """)


class TestMobileToolbarLayout:
    def test_long_org_name_cannot_wrap_launch_control(self, h):
        ab_raw("set", "viewport", "320", "900")
        ready = ab_eval("""
            var root = document.querySelector('[x-data="sessionsPage()"]');
            var d = root && root._x_dataStack && root._x_dataStack[0];
            if (!d) return null;
            window.__toolbarOrgState = {list: d.orgFilterList, selected: d.selectedOrg};
            d.orgFilterList = [{
              slug:'dynamic-benchmarking',
              name:'Dynamic Benchmarking Organization With A Very Long Name',
              color:'#10b981', initial:'D', favicon:null,
            }];
            d.selectedOrg = 'dynamic-benchmarking';
            return true;
        """)
        assert ready
        time.sleep(0.2)
        state = ab_eval("""
            var toolbar = document.querySelector('[data-testid="sessions-page-toolbar"]');
            var launch = document.querySelector('[data-testid="session-launch-dropdown"]');
            var value = document.querySelector('.sessions-org-filter-value');
            var tr = toolbar.getBoundingClientRect();
            var lr = launch.getBoundingClientRect();
            return {
              sameRow: lr.top >= tr.top && lr.bottom <= tr.bottom + 1,
              launchRight: lr.right,
              viewport: window.innerWidth,
              truncated: value.scrollWidth > value.clientWidth,
            };
        """)
        assert state["sameRow"], "Create-workspace control wrapped below the toolbar"
        assert state["launchRight"] <= state["viewport"]
        assert state["truncated"], "Long organization name was not ellipsized"

        ab_eval("""
            var root = document.querySelector('[data-testid="session-launch-dropdown"]');
            root.querySelector('button').click();
            return true;
        """)
        time.sleep(0.2)
        menu = ab_eval("""
            var root = document.querySelector('[data-testid="session-launch-dropdown"]');
            var panel = root.querySelector('[x-show="open"]');
            var r = panel.getBoundingClientRect();
            return {left:r.left, right:r.right, width:r.width, viewport:window.innerWidth};
        """)
        assert menu["width"] > 0, "Create-workspace menu did not open"
        assert menu["left"] >= 0 and menu["right"] <= menu["viewport"]

        ab_eval("""
            var launch = document.querySelector('[data-testid="session-launch-dropdown"]');
            launch.querySelector('button').click();
            var root = document.querySelector('[x-data="sessionsPage()"]');
            var d = root._x_dataStack[0];
            d.orgFilterList = window.__toolbarOrgState.list;
            d.selectedOrg = window.__toolbarOrgState.selected;
            delete window.__toolbarOrgState;
            return true;
        """)
        ab_raw("set", "viewport", "430", "900")


class TestDesktopToolbarLayout:
    def test_actions_follow_zoom_control_without_consuming_page_width(self, h):
        ab_raw("set", "viewport", "1440", "900")
        layout = ab_eval("""
            var toolbar = document.querySelector('[data-testid="sessions-page-toolbar"]');
            var zoom = toolbar && toolbar.querySelector('.sc-zoom-bar');
            var actions = toolbar && toolbar.querySelector('.sessions-toolbar-actions');
            if (!zoom || !actions) return null;
            var zr = zoom.getBoundingClientRect();
            var ar = actions.getBoundingClientRect();
            return {gap: ar.left - zr.right, toolbarWidth: toolbar.getBoundingClientRect().width};
        """)
        assert layout is not None
        assert layout["gap"] <= 16, (
            "The organization and create-session controls were distributed across "
            "the desktop toolbar instead of remaining beside zoom."
        )
        assert layout["toolbarWidth"] > 1000


class TestRecentSessions:
    """Recent sessions section shows historical sessions."""

    def test_heading_visible(self, h):
        text = h.visible_text()
        assert "Recent Sessions" in text

    def test_entries_visible(self, h):
        text = h.visible_text()
        # Recent session titles from fixture
        assert "alpha history" in text.lower() or "beta history" in text.lower()


class TestRecentCardActionsMenu:
    """Recent cards use an actions menu triggered by the org icon (auto-ycry3).

    Resume is a menu entry now — no standalone green button on the card. The
    org-icon slot opens `window.actionSheet` with Resume + Open entries.
    """

    def test_no_standalone_resume_button_on_recent_cards(self, h):
        """data-testid='resume-btn' must not appear inside recent-session-row."""
        count = ab_eval("""
            var recent = document.querySelectorAll('[data-testid="recent-session-row"]');
            var c = 0;
            recent.forEach(function(row) {
              if (row.querySelector('[data-testid="resume-btn"]')) c++;
            });
            return c;
        """)
        assert count == 0, f"Found {count} standalone Resume button(s) on Recent cards"

    def test_recent_cards_have_session_actions_btn(self, h):
        """Every Recent card exposes the merged sc-org sc-actions button."""
        count = ab_eval("""
            var recent = document.querySelectorAll('[data-testid="recent-session-row"]');
            var n = 0;
            recent.forEach(function(row) {
              if (row.querySelector('[data-testid="session-actions-btn"]')) n++;
            });
            return {recent: recent.length, with_btn: n};
        """)
        assert count["recent"] > 0, "No recent-session-row elements rendered"
        assert count["with_btn"] == count["recent"], (
            f"Only {count['with_btn']}/{count['recent']} Recent cards carry the "
            "session-actions-btn"
        )

    def test_recent_loop_uses_div_not_anchor(self, h):
        """The Recent loop emits <div> rows, not <a href> rows."""
        tag = ab_eval("""
            var rows = document.querySelectorAll('[data-testid="recent-session-row"]');
            if (rows.length === 0) return null;
            return rows[0].tagName.toUpperCase();
        """)
        assert tag == "DIV", f"Expected DIV row, got {tag}"


class TestRecentSortHasDuration:
    """Sort dropdown includes Duration (auto-ycry3)."""

    def test_duration_option_exists(self, h):
        """The Recent sort toggle must list Duration as a selectable option.

        The dropdown options are only emitted into the DOM once the toggle is
        open, so click the trigger first, then read the menu contents.
        """
        # Click the toggle to open its menu, then read the rendered options.
        result = ab_eval("""
            var btn = document.querySelector('[data-testid="recent-sort-toggle"]');
            if (btn) btn.click();
            return btn ? true : false;
        """)
        assert result, "recent-sort-toggle button not found"
        # Give Alpine a tick to render the menu
        import time as _t
        _t.sleep(0.5)
        options = ab_eval("""
            var menu = document.querySelector('[data-testid="recent-sort-toggle-menu"]');
            if (!menu) return [];
            return Array.from(menu.querySelectorAll('.sort-option'))
              .map(function(n) { return n.textContent.trim(); });
        """) or []
        found_duration = any("Duration" in (t or "") for t in options)
        assert found_duration, f"Duration option not rendered; got options={options!r}"
        # Dismiss the menu so it doesn't affect subsequent tests
        ab_eval("""
            var btn = document.querySelector('[data-testid="recent-sort-toggle"]');
            if (btn) btn.click();
            return true;
        """)


class TestLibrarianRecentTitle:
    """Librarian recent cards render a meaningful title (auto-ycry3).

    The raw process name ('librarian-review_report-PID-JOBID') must not be the
    card's displayed label. When the target bead is resolvable, the label
    reads '{type} · {bead_id}'.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def librarian_fixture_ready(cls, h):
        fixture = sessions_page_fixture()
        fixture["recent_sessions"] = list(fixture["recent_sessions"]) + [
            {
                "id": "src-lib-recent",
                "type": "session",
                "session_type": "librarian",
                "title": "librarian-review_report-12345-deadbeef",
                "project": "autonomy",
                "tmux_session": "librarian-review_report-12345-deadbeef",
                "librarian_type": "review_report",
                "librarian_target_bead_id": "auto-targetx",
                "librarian_target_bead_title": "Fix X behaviour",
                "last_activity_at": _iso_inline(minutes_ago=5),
                "created_at": _iso_inline(minutes_ago=25),
                "entry_count": 7,
                "context_tokens": 2500,
            }
        ]
        h.set_fixture(fixture)
        h.open_sessions_page()
        time.sleep(2)
        yield
        # Restore default fixture after the class finishes
        h.set_fixture(sessions_page_fixture())
        h.open_sessions_page()

    def test_librarian_title_is_not_raw_process_name(self, h, librarian_fixture_ready):
        """The visible title must NOT match the raw `librarian-...` pattern."""
        titles = ab_eval("""
            var rows = document.querySelectorAll('[data-testid="recent-session-row"]');
            return Array.from(rows).map(function(row) {
              var t = row.querySelector('.sc-title');
              return t ? t.textContent.trim() : '';
            });
        """) or []
        offenders = [t for t in titles if t.startswith("librarian-")]
        assert not offenders, (
            f"Librarian cards still show raw process names: {offenders!r}"
        )


class TestEmptyState:
    """When there are no sessions, show an appropriate message."""

    def test_empty_message_shown(self, h):
        h.set_fixture(fixtures.empty_sessions())
        h.open_sessions_page()
        time.sleep(2)
        text = h.visible_text()
        assert any(msg in text for msg in (
            "No active sessions",
            "No recent sessions",
            "No sessions found",
            "No sessions",
        ))

    def test_state_restored(self, h):
        h.set_fixture(sessions_page_fixture())
        h.open_sessions_page()
        time.sleep(2)
        count = h.card_count()
        assert count >= 5
