"""
HTTP-level functional tests for the sessions page.

Tests the /api/dao/active_sessions, /api/dao/recent_sessions endpoints and
the /sessions HTML page. Also validates JS wiring by reading source files.
No browser needed — uses TestClient.
"""
from pathlib import Path

import pytest

from tools.dashboard.tests.sessions.conftest import SESSIONS_PAGE_SESSIONS


JS_DIR = Path(__file__).resolve().parents[2] / "static" / "js"
SESSIONS_TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "pages" / "sessions.html"


# ── Active Sessions API ─────────────────────────────────────────────


class TestActiveSessionsAPI:
    """GET /api/dao/active_sessions returns the mock registry."""

    def test_returns_sessions(self, test_client):
        resp = test_client.get("/api/dao/active_sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 5

    def test_has_card_fields(self, test_client):
        resp = test_client.get("/api/dao/active_sessions")
        session = resp.json()[0]
        for field in ("session_id", "label", "type", "is_live", "entry_count", "context_tokens"):
            assert field in session, f"missing field: {field}"

    def test_labels_populated(self, test_client):
        data = test_client.get("/api/dao/active_sessions").json()
        labeled = [s for s in data if s.get("label")]
        assert len(labeled) >= 5

    def test_roles_populated(self, test_client):
        data = test_client.get("/api/dao/active_sessions").json()
        with_role = [s for s in data if s.get("role")]
        assert len(with_role) >= 4

    def test_host_type_present(self, test_client):
        data = test_client.get("/api/dao/active_sessions").json()
        hosts = [s for s in data if s.get("type") == "host"]
        assert len(hosts) >= 1

    def test_topics_present_as_arrays(self, test_client):
        data = test_client.get("/api/dao/active_sessions").json()
        with_topics = [s for s in data if isinstance(s.get("topics"), list) and len(s["topics"]) > 0]
        assert len(with_topics) >= 3, "expected at least 3 sessions with topic arrays"

    def test_entry_counts(self, test_client):
        data = test_client.get("/api/dao/active_sessions").json()
        counts = {s["session_id"]: s["entry_count"] for s in data}
        assert counts.get("auto-test-alpha") == 150
        assert counts.get("auto-test-beta") == 200

    def test_context_tokens(self, test_client):
        data = test_client.get("/api/dao/active_sessions").json()
        tokens = {s["session_id"]: s["context_tokens"] for s in data}
        assert tokens.get("host-test-delta") == 250000
        assert tokens.get("auto-test-epsilon") == 30000

    def test_nag_fields(self, test_client):
        data = test_client.get("/api/dao/active_sessions").json()
        gamma = next(s for s in data if s["session_id"] == "auto-test-gamma")
        assert gamma["nag_enabled"] is True
        assert gamma["nag_interval"] == 10
        assert gamma["nag_message"] == "Check review status"

    def test_dispatch_nag_enabled_present(self, test_client):
        """Every session row carries dispatch_nag_enabled: bool, defaulting to False."""
        data = test_client.get("/api/dao/active_sessions").json()
        assert len(data) >= 1
        for s in data:
            assert "dispatch_nag_enabled" in s, f"missing field on {s['session_id']}"
            assert isinstance(s["dispatch_nag_enabled"], bool)
        # Fixture has no session opted in → all default to False
        assert all(s["dispatch_nag_enabled"] is False for s in data)


class TestHarnessUsageAPI:
    """GET /api/harness_usage summarizes live harness rate-limit telemetry."""

    def test_returns_live_harness_tiles(self, test_client):
        resp = test_client.get("/api/harness_usage")
        assert resp.status_code == 200
        data = resp.json()
        assert "harnesses" in data

        by_harness = {item["harness"]: item for item in data["harnesses"]}
        assert "claude" in by_harness
        assert "codex" in by_harness

    def test_surfaces_codex_rate_limits_and_claude_gap(self, test_client):
        data = test_client.get("/api/harness_usage").json()
        by_harness = {item["harness"]: item for item in data["harnesses"]}

        codex = by_harness["codex"]
        assert codex["available"] is True
        assert codex["session_count"] == 1
        assert codex["state"]["plan_type"] == "pro"
        assert codex["state"]["windows"]["short"]["used_percent"] == 2.0
        assert codex["state"]["windows"]["long"]["used_percent"] == 10.0

        claude = by_harness["claude"]
        assert claude["available"] is False
        assert claude["session_count"] == 4
        assert "reason" in claude


# ── Recent Sessions API ─────────────────────────────────────────────


class TestRecentSessionsAPI:
    """GET /api/dao/recent_sessions returns graph.db session sources."""

    def test_returns_recent_sessions(self, test_client):
        resp = test_client.get("/api/dao/recent_sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 3

    def test_has_required_fields(self, test_client):
        data = test_client.get("/api/dao/recent_sessions").json()
        session = data[0]
        for field in ("id", "type", "date", "title"):
            assert field in session, f"missing field: {field}"

    def test_cold_history_response_advises_a_bounded_retry(self, test_client, monkeypatch):
        """A cold cache should tell the async client when to retry, not invite rapid polling."""
        from tools.dashboard import server

        monkeypatch.setattr(server.dao_sessions, "recent_sessions_cached", lambda *args: None)
        response = test_client.get("/api/dao/recent_sessions")
        assert response.status_code == 202
        assert response.headers["retry-after"] == "5"

    def test_limit_param_is_deprecated(self, test_client):
        """`limit` is retired in favour of server-side per-type quotas (auto-wyo79).

        Passing it must not raise, and must not shrink the payload below the
        per-type quota budget.
        """
        no_limit = test_client.get("/api/dao/recent_sessions").json()
        with_limit = test_client.get("/api/dao/recent_sessions?limit=2").json()
        assert len(no_limit) == len(with_limit), \
            f"limit=2 changed row count ({len(no_limit)} vs {len(with_limit)})"

    def test_org_scoped_session_cannot_read_another_org(self, test_client, monkeypatch):
        """An org-stamped agent cannot turn ``?org=`` into a cross-org read."""
        from tools.dashboard import server

        monkeypatch.setattr(
            server,
            "authenticate_session_request",
            lambda request: (("auto-dynbench", "dynbench"), None),
        )
        response = test_client.get("/api/dao/recent_sessions?org=anchore")
        assert response.status_code == 403

    def test_selected_org_is_forwarded_to_the_scoped_cache(self, test_client, monkeypatch):
        """The endpoint scopes the DAO cache key instead of post-filtering rows."""
        from tools.dashboard import server

        captured = {}

        def fake_cached(sort, since, type_group, org):
            captured.update(sort=sort, since=since, type_group=type_group, org=org)
            return []

        monkeypatch.setattr(server, "_token_org_or_none", lambda request: None)
        monkeypatch.setattr(server.dao_sessions, "recent_sessions_cached", fake_cached)
        response = test_client.get(
            "/api/dao/recent_sessions?org=dynbench&type=interactive&since=1d"
        )
        assert response.status_code == 200
        assert captured == {
            "sort": "lastActivity",
            "since": "1d",
            "type_group": "interactive",
            "org": "dynbench",
        }


class TestSessionStatusAPI:
    """GET /api/dao/session_status powers ``graph sessions --status``."""

    def test_returns_live_rows_without_since(self, test_client):
        resp = test_client.get("/api/dao/session_status")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == len(SESSIONS_PAGE_SESSIONS)
        assert all(row["is_live"] == 1 for row in data)

    def test_since_includes_recent_dead_rows(self, test_client):
        data = test_client.get("/api/dao/session_status?since=24h").json()
        tmux_names = {row["tmux_name"] for row in data}
        assert "auto-test-alpha" in tmux_names
        assert "auto-recent-alpha" in tmux_names

    def test_invalid_since_returns_400(self, test_client):
        resp = test_client.get("/api/dao/session_status?since=bogus")
        assert resp.status_code == 400
        assert "Invalid duration" in resp.json()["error"]


# ── Sessions Page HTML ──────────────────────────────────────────────


class TestSessionsPageHTML:
    """/sessions returns the page shell; /pages/sessions has the Alpine template."""

    def test_sessions_returns_200(self, test_client):
        resp = test_client.get("/sessions")
        assert resp.status_code == 200

    def test_shell_stamps_default_graph_org_for_schema_reads(self, test_client):
        resp = test_client.get("/beads")
        assert resp.status_code == 200
        assert '<meta name="autonomy-shell-org" content="autonomy">' in resp.text

    def test_shell_graph_org_falls_back_to_autonomy_without_env(self, test_client, monkeypatch):
        monkeypatch.delenv("GRAPH_SCOPE", raising=False)
        resp = test_client.get("/beads")
        assert resp.status_code == 200
        assert '<meta name="autonomy-shell-org" content="autonomy">' in resp.text

    def test_has_alpine_component(self, test_client):
        resp = test_client.get("/pages/sessions")
        assert resp.status_code == 200
        html = resp.text
        assert 'sessionsPage()' in html

    def test_has_card_template_with_testid(self, test_client):
        resp = test_client.get("/pages/sessions")
        html = resp.text
        assert 'data-testid="session-card"' in html

    def test_has_topics_binding(self, test_client):
        resp = test_client.get("/pages/sessions")
        html = resp.text
        assert "s.topics" in html
        assert "sc-topic" in html


class TestRecentLoopWiring:
    """The Recent loop uses a div + @click, not an <a> wrapper (auto-ycry3).

    A div + @click lets the merged sc-org sc-actions button stop propagation
    to open the action sheet without triggering the row-level navigation.
    """

    def test_recent_loop_is_a_div_not_an_anchor(self, test_client):
        html = test_client.get("/pages/sessions").text
        # The Recent loop's wrapper carries recent-session-row; it must be a
        # <div ... data-testid="recent-session-row">, not <a ...>.
        import re
        # Search for <a ... recent-session-row — should NOT match
        assert not re.search(
            r"<a[^>]+data-testid=\"recent-session-row\"", html, re.DOTALL
        ), "Recent loop still uses <a href> wrapper; must be a <div @click>"

    def test_recent_loop_uses_click_navigate(self, test_client):
        html = test_client.get("/pages/sessions").text
        # The new <div> must bind @click="navigate(s)"
        assert 'data-testid="recent-session-row"' in html
        # The @click attribute appears as either @click or x-on:click depending
        # on build; normalise by checking for 'navigate(s)' in that neighbourhood.
        import re
        m = re.search(
            r'<div[^>]+@click\s*=\s*"navigate\(s\)"[^>]*data-testid="recent-session-row"',
            html,
            re.DOTALL,
        )
        assert m, "Recent loop div does not bind @click=\"navigate(s)\""


# ── JS Wiring (text analysis of source files) ───────────────────────


class TestSessionsJSWiring:
    """Verify sessions.js and session-store.js contain expected patterns."""

    @pytest.fixture(autouse=True)
    def _load_js(self):
        self.sessions_js = (JS_DIR / "pages" / "sessions.js").read_text()
        self.store_js = (JS_DIR / "lib" / "session-store.js").read_text()

    def test_update_from_store_reads_topics(self):
        # _updateFromStore should read topics from the Alpine store
        assert "s.topics" in self.sessions_js

    def test_seed_sets_topics(self):
        # Session store seed (moved from sessions.js) must set store.topics
        # This is the acceptance test for the topics bug fix
        assert "store.topics" in self.store_js, (
            "session-store.js seed does not set store.topics — topics bug not fixed"
        )

    def test_seed_sets_nag_fields(self):
        # Session store seed must set nagEnabled
        assert "store.nagEnabled" in self.store_js, (
            "session-store.js seed does not set store.nagEnabled"
        )

    def test_navigate_exists(self):
        assert "navigate(" in self.sessions_js

    def test_session_store_has_topics_default(self):
        assert "topics: []" in self.store_js

    def test_org_selection_refetches_its_server_scoped_recent_history(self):
        assert "this.selectedOrg = slug;" in self.sessions_js
        assert "this._fetchRecent();" in self.sessions_js
        assert "selectedOrg ? '&org=' + encodeURIComponent(selectedOrg) : ''" in self.sessions_js

    def test_recent_history_loading_is_visible_but_nonblocking(self):
        """Regression: a background history fetch had no visible progress state."""
        template = SESSIONS_TEMPLATE.read_text()
        assert 'data-testid="recent-history-loading"' in template
        assert 'x-show="recentLoading"' in template
        assert "Loading history…" in template

    def test_registry_events_update_ended_cards_without_refetching_history(self):
        """Regression: every registry event made an immediate and delayed DAO request."""
        assert "_applyEndedSessions(e && e.detail && e.detail.endedSessions)" in self.sessions_js
        assert "_scheduleRecentRefresh" not in self.sessions_js
        assert "_recentEchoTimer" not in self.sessions_js
        assert "detail: { endedSessions: endedSessions || [] }" in self.store_js
        assert "store.graphSourceId = s.graph_source_id || '';" in self.store_js

    def test_duplicate_history_fetches_share_one_inflight_request(self):
        """Regression: identical filter state could start redundant concurrent fetches."""
        assert "this._recentRequest && this._recentRequest.key === queryKey" in self.sessions_js
        assert "return this._recentRequest.promise;" in self.sessions_js
        assert "response.headers.get('Retry-After')" in self.sessions_js

    def test_restart_action_precedes_close_and_calls_atomic_endpoint(self):
        restart = self.sessions_js.index("label: 'Restart Session'")
        close = self.sessions_js.index("label: 'Close Session'")
        assert restart < close
        assert "encodeURIComponent(tmux) + '/restart'" in self.sessions_js
