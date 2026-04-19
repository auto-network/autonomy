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

    def test_limit_param_is_deprecated(self, test_client):
        """`limit` is retired in favour of server-side per-type quotas (auto-wyo79).

        Passing it must not raise, and must not shrink the payload below the
        per-type quota budget.
        """
        no_limit = test_client.get("/api/dao/recent_sessions").json()
        with_limit = test_client.get("/api/dao/recent_sessions?limit=2").json()
        assert len(no_limit) == len(with_limit), \
            f"limit=2 changed row count ({len(no_limit)} vs {len(with_limit)})"


# ── Sessions Page HTML ──────────────────────────────────────────────


class TestSessionsPageHTML:
    """/sessions returns the page shell; /pages/sessions has the Alpine template."""

    def test_sessions_returns_200(self, test_client):
        resp = test_client.get("/sessions")
        assert resp.status_code == 200

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
