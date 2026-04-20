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

    def test_tail_returns_resolved_field(self, test_client):
        """Tail response must include 'resolved' so the viewer state machine
        can enter 'live' state without waiting for SSE registry."""
        data = test_client.get("/api/session/autonomy/auto-test-designer/tail?after=0").json()
        assert "resolved" in data, f"Missing 'resolved' in tail response: {list(data.keys())}"
        assert data["resolved"] is True, "Mock sessions with entries should be resolved"

    def test_nonexistent_session_handled(self, test_client):
        resp = test_client.get("/api/session/autonomy/doesnt-exist/tail?after=0")
        assert resp.status_code in (200, 400, 404), f"Unexpected: {resp.status_code}"


# ── Session Role API ──────────────────────────────────────────────────

class TestSessionRoleAPI:

    def test_set_valid_role(self, test_client):
        resp = test_client.put("/api/session/auto-test-designer/role", json={"role": "builder"})
        assert resp.status_code == 200

    def test_set_custom_role(self, test_client):
        resp = test_client.put("/api/session/auto-test-designer/role", json={"role": "analyst"})
        assert resp.status_code == 200

    def test_clear_role(self, test_client):
        resp = test_client.put("/api/session/auto-test-designer/role", json={"role": ""})
        assert resp.status_code == 200

    def test_reject_role_too_long(self, test_client):
        resp = test_client.put("/api/session/auto-test-designer/role", json={"role": "a" * 33})
        assert resp.status_code == 400
        assert "max 32" in resp.json()["error"]


# ── Connect/Disconnect Wiring ─────────────────────────────────────────

class TestConnectWiring:
    """Does _connectSession actually initialize the chat panel?
    These read design.js as text — no browser needed, instant results."""

    def _get_function_body(self, func_name, end_marker):
        repo_root = Path(__file__).resolve().parents[4]
        js = (repo_root / "tools/dashboard/static/js/pages/design.js").read_text()
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


# ── Queued Message Dedup ─────────────────────────────────────────────

class TestDedupQueuedEntries:
    """_dedup_queued_entries removes duplicate user/crosstalk entries that
    follow a queued version (queue-operation + user for the same text)."""

    @staticmethod
    def _dedup(entries):
        from tools.dashboard.server import _dedup_queued_entries
        return _dedup_queued_entries(entries)

    def test_queued_user_deduped(self):
        """Queue-operation entry kept, following identical user entry removed."""
        entries = [
            {"type": "user", "content": "queued msg", "queued": True},
            {"type": "user", "content": "queued msg"},
        ]
        result = self._dedup(entries)
        assert len(result) == 1
        assert result[0]["queued"] is True

    def test_queued_crosstalk_deduped(self):
        """Crosstalk variant: queue-operation crosstalk kept, duplicate removed."""
        entries = [
            {"type": "crosstalk", "content": "hey from other session", "queued": True},
            {"type": "crosstalk", "content": "hey from other session"},
        ]
        result = self._dedup(entries)
        assert len(result) == 1
        assert result[0]["queued"] is True

    def test_normal_messages_unaffected(self):
        """Non-queued messages pass through untouched."""
        entries = [
            {"type": "user", "content": "first"},
            {"type": "assistant_text", "content": "reply"},
            {"type": "user", "content": "second"},
        ]
        result = self._dedup(entries)
        assert len(result) == 3

    def test_whitespace_match(self):
        """Content with extra whitespace still matches."""
        entries = [
            {"type": "user", "content": "  hello world  ", "queued": True},
            {"type": "user", "content": "hello world"},
        ]
        result = self._dedup(entries)
        assert len(result) == 1

    def test_different_content_not_deduped(self):
        """A queued entry followed by a different user entry: both kept."""
        entries = [
            {"type": "user", "content": "queued msg", "queued": True},
            {"type": "user", "content": "different msg"},
        ]
        result = self._dedup(entries)
        assert len(result) == 2

    def test_intervening_assistant_does_not_reset_tracker(self):
        """An assistant entry between queued and user does NOT reset the tracker.

        In real Claude Code JSONL, the duplicate user entry typically arrives
        AFTER the agent's assistant turn, not immediately after the
        queue-operation. The tracker survives intervening assistant/tool_result
        entries so the dup-user is still identified and deduped. See
        _dedup_queued_entries in server.py.
        """
        entries = [
            {"type": "user", "content": "queued msg", "queued": True},
            {"type": "assistant_text", "content": "reply"},
            {"type": "user", "content": "queued msg"},
        ]
        result = self._dedup(entries)
        # Assistant reply is preserved; the duplicate user is deduped.
        assert len(result) == 2

    def test_empty_input(self):
        assert self._dedup([]) == []

    def test_queued_only_no_crash(self):
        """A queued entry with no following duplicate is kept."""
        entries = [{"type": "user", "content": "solo queued", "queued": True}]
        result = self._dedup(entries)
        assert len(result) == 1
