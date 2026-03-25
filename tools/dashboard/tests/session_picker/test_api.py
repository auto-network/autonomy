"""
HTTP-level functional tests for the session picker data layer.

Tests the APIs and JS wiring that power the session picker.
No browser needed — uses TestClient for HTTP, text analysis for JS.
"""
import pytest
from pathlib import Path


# ── Active Sessions API ───────────────────────────────────────────────

class TestActiveSessionsAPI:
    """Does /api/dao/active_sessions return the data the picker needs?"""

    def test_returns_sessions(self, test_client):
        data = test_client.get("/api/dao/active_sessions").json()
        assert len(data) >= 4, f"Expected 4+ sessions, got {len(data)}"

    def test_sessions_have_fields_picker_needs(self, test_client):
        """The picker binds to these fields — if any are missing, the UI breaks."""
        data = test_client.get("/api/dao/active_sessions").json()
        needed = {"session_id", "label", "type", "is_live", "last_message"}
        for s in data:
            missing = needed - set(s.keys())
            assert not missing, f"{s.get('session_id','?')} missing: {missing}"

    def test_labels_populated(self, test_client):
        """Sessions with labels set should return them — the picker shows labels, not tmux names."""
        data = test_client.get("/api/dao/active_sessions").json()
        labeled = [s for s in data if s.get("label")]
        assert len(labeled) >= 3

    def test_roles_populated(self, test_client):
        data = test_client.get("/api/dao/active_sessions").json()
        with_roles = [s for s in data if s.get("role")]
        assert len(with_roles) >= 2

    def test_host_sessions_have_type(self, test_client):
        """Host sessions need type='host' so the picker can style them differently."""
        data = test_client.get("/api/dao/active_sessions").json()
        hosts = [s for s in data if s.get("type") == "host"]
        assert len(hosts) >= 1


# ── Session Tail API ──────────────────────────────────────────────────

class TestSessionTailAPI:
    """Does the tail API return parsed entries for the chat panel?"""

    def test_returns_entries_for_session_with_jsonl(self, test_client):
        data = test_client.get("/api/session/autonomy/auto-test-designer/tail?after=0").json()
        entries = data.get("entries", [])
        assert len(entries) > 0, "No entries despite JSONL having data"

    def test_entries_are_parsed(self, test_client):
        """Entries should be parsed into typed objects, not raw JSONL lines."""
        data = test_client.get("/api/session/autonomy/auto-test-designer/tail?after=0").json()
        types = {e.get("type") for e in data.get("entries", [])}
        assert "assistant_text" in types, f"Expected parsed types, got: {types}"

    def test_nonexistent_session_handled(self, test_client):
        resp = test_client.get("/api/session/autonomy/doesnt-exist/tail?after=0")
        assert resp.status_code in (200, 400, 404), f"Unexpected: {resp.status_code}"


# ── Session Role API ──────────────────────────────────────────────────

class TestSessionRoleAPI:

    def test_set_valid_role(self, test_client):
        resp = test_client.put("/api/session/auto-test-designer/role", json={"role": "builder"})
        assert resp.status_code == 200

    def test_reject_invalid_role(self, test_client):
        resp = test_client.put("/api/session/auto-test-designer/role", json={"role": "supreme_overlord"})
        assert resp.status_code == 400


# ── Connect/Disconnect Wiring ─────────────────────────────────────────

class TestConnectWiring:
    """Does _connectSession actually initialize the chat panel?
    These read experiment.js as text — no browser needed, instant results."""

    def _get_function_body(self, func_name, end_marker):
        repo_root = Path(__file__).resolve().parents[4]
        js = (repo_root / "tools/dashboard/static/js/pages/experiment.js").read_text()
        start = js.find(f"{func_name}:")
        end = js.find(f"{end_marker}:", start)
        assert start != -1, f"{func_name} not found"
        return js[start:end]

    def test_connect_persists_to_localstorage(self):
        body = self._get_function_body("_connectSession", "disconnectSession")
        assert "localStorage" in body

    def test_connect_initializes_chat_panel(self):
        """The connected session must be wired to the chat panel so it loads entries.
        FAILS until the bug is fixed — this is the acceptance test."""
        body = self._get_function_body("_connectSession", "disconnectSession")
        has_wiring = (
            "configure" in body
            or "getSessionStore" in body
            or "ensureSessionMessages" in body
            or "/tail" in body
        )
        assert has_wiring, (
            "BUG: _connectSession sets flags but never initializes the chat panel. "
            "See postmortem graph://fc8b4f21-1d7"
        )

    def test_disconnect_clears_state(self):
        body = self._get_function_body("disconnectSession", "_loadChatSessions")
        assert "chatConnected" in body, "Disconnect doesn't reset connection flag"
        assert "localStorage.removeItem" in body, "Disconnect doesn't clear persistence"

    def test_disconnect_reloads_picker(self):
        body = self._get_function_body("disconnectSession", "_loadChatSessions")
        assert "_loadChatSessions" in body, "Disconnect doesn't reload the session list"
