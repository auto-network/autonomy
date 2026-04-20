"""auto-opbyh test #6 — dispatcher uses HTTP, not direct DB write.

The dispatcher process is separate from the dashboard process. Any code in
agents/dispatcher.py that tries to touch dashboard.db directly bypasses
session_monitor's in-process state (inotify watches, SSE broadcasts).

FAIL-REASON on master: agents/dispatcher.py::_upsert_monitor_row imports
tools.dashboard.dao.dashboard_db and calls upsert_session() directly. No
HTTP client involved. Test asserts the opposite: HTTP client was called,
dashboard_db.upsert_session was NOT.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


class TestDispatcherHTTPClient:
    """#6 — dispatcher helpers post to /api/monitor/* instead of DB-direct."""

    def test_dispatcher_register_uses_http_not_db(self, tmp_path, monkeypatch):
        """_register_dispatch_session must make an HTTP POST, not a DB upsert."""
        from agents import dispatcher

        # Fake agent + jsonl path
        class FakeAgent:
            def __init__(self):
                self.bead_id = "auto-x-http"
                self.output_dir = str(tmp_path / "agent-runs" / "auto-x-http-20260420-010101")

        (tmp_path / "agent-runs" / "auto-x-http-20260420-010101" / "sessions" / "autonomy").mkdir(parents=True)
        jsonl = (
            tmp_path / "agent-runs" / "auto-x-http-20260420-010101"
            / "sessions" / "autonomy" / "some-uuid.jsonl"
        )
        jsonl.write_text("")

        # Mock the dashboard_db module so we can assert it is NOT touched.
        # Mock urllib.request.urlopen OR requests.post — whichever the impl
        # chooses. We test both common stdlib paths.
        fake_urlopen = MagicMock()
        fake_urlopen.return_value.__enter__ = MagicMock(
            return_value=MagicMock(read=MagicMock(return_value=b'{"ok":true}'))
        )
        fake_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        fake_upsert = MagicMock()
        fake_mark_dead = MagicMock()

        agent = FakeAgent()

        # Patch both potential DB entry points
        with patch("tools.dashboard.dao.dashboard_db.upsert_session", fake_upsert), \
             patch("tools.dashboard.dao.dashboard_db.mark_dead", fake_mark_dead), \
             patch("urllib.request.urlopen", fake_urlopen):
            dispatcher._register_dispatch_session(agent, jsonl)

        # Assert DB was NOT touched
        assert fake_upsert.call_count == 0, (
            f"dispatcher._register_dispatch_session called "
            f"dashboard_db.upsert_session() {fake_upsert.call_count} time(s). "
            "It MUST go through HTTP to /api/monitor/register — the direct DB "
            "write bypasses session_monitor's in-process state (inotify + SSE). "
            "This is the auto-ylj6r gap."
        )

        # Assert HTTP client was invoked
        assert fake_urlopen.call_count >= 1, (
            "dispatcher._register_dispatch_session did not call urllib.request.urlopen. "
            "The dispatcher must POST to /api/monitor/register via HTTP so the "
            "dashboard process receives the signal to call session_monitor."
            "register_session() in-process."
        )

        # Inspect the request: must be POST to /api/monitor/register
        call = fake_urlopen.call_args
        req_arg = call[0][0] if call.args else call.kwargs.get("req") or call.kwargs.get("url")
        # req_arg is either a urllib.request.Request or a URL string
        if hasattr(req_arg, "full_url"):
            url = req_arg.full_url
            method = req_arg.get_method()
        else:
            url = str(req_arg)
            method = "POST"  # can't easily introspect a non-Request arg
        assert "/api/monitor/register" in url, (
            f"HTTP call URL did not target /api/monitor/register; got {url!r}"
        )
        assert method.upper() == "POST", f"method was {method!r}, expected POST"

    def test_dispatcher_deregister_uses_http_not_db(self, tmp_path, monkeypatch):
        """_deregister_session_with_monitor must POST, not call mark_dead."""
        from agents import dispatcher

        fake_urlopen = MagicMock()
        fake_urlopen.return_value.__enter__ = MagicMock(
            return_value=MagicMock(read=MagicMock(return_value=b'{"ok":true}'))
        )
        fake_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        fake_upsert = MagicMock()
        fake_mark_dead = MagicMock()

        with patch("tools.dashboard.dao.dashboard_db.upsert_session", fake_upsert), \
             patch("tools.dashboard.dao.dashboard_db.mark_dead", fake_mark_dead), \
             patch("urllib.request.urlopen", fake_urlopen):
            dispatcher._deregister_session_with_monitor("auto-http-deregister-001")

        assert fake_mark_dead.call_count == 0, (
            f"dispatcher._deregister_session_with_monitor called "
            f"dashboard_db.mark_dead() {fake_mark_dead.call_count} time(s). "
            "It MUST go through HTTP to /api/monitor/deregister."
        )
        assert fake_urlopen.call_count >= 1, (
            "dispatcher did not POST to /api/monitor/deregister."
        )

        call = fake_urlopen.call_args
        req_arg = call[0][0] if call.args else call.kwargs.get("req") or call.kwargs.get("url")
        if hasattr(req_arg, "full_url"):
            url = req_arg.full_url
        else:
            url = str(req_arg)
        assert "/api/monitor/deregister" in url, (
            f"deregister URL did not target /api/monitor/deregister; got {url!r}"
        )
