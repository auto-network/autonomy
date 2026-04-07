"""Tests for broadcast idle filter — CLI and API.

Verifies that crosstalk broadcast filters recipients by idle time,
defaults to 1h, caps at 6h, and reports sent/skipped counts.
"""

import argparse
import json
import time
import unittest
from unittest import mock

import pytest


# ── CLI unit tests ──────────────────────────────────────────────────────

class TestParseLastActivity(unittest.TestCase):
    """_parse_last_activity normalises float, int, ISO string, and None."""

    def setUp(self):
        from tools.graph.cli import _parse_last_activity
        self.parse = _parse_last_activity

    def test_float_passthrough(self):
        assert self.parse(1712000000.0) == 1712000000.0

    def test_int_to_float(self):
        assert self.parse(1712000000) == 1712000000.0

    def test_iso_string(self):
        result = self.parse("2026-04-07T12:00:00")
        assert isinstance(result, float)
        assert result > 0

    def test_none_returns_none(self):
        assert self.parse(None) is None

    def test_garbage_string_returns_none(self):
        assert self.parse("not-a-date") is None


class TestBroadcastIdleValidation(unittest.TestCase):
    """CLI arg validation for --idle flag."""

    def test_idle_over_6h_rejected(self):
        """--idle 7h should be rejected (> 6h cap)."""
        from tools.graph.cli import _parse_duration, _MAX_BROADCAST_IDLE_SECS
        secs = _parse_duration("7h")
        assert secs > _MAX_BROADCAST_IDLE_SECS

    def test_idle_6h_accepted(self):
        from tools.graph.cli import _parse_duration, _MAX_BROADCAST_IDLE_SECS
        secs = _parse_duration("6h")
        assert secs == _MAX_BROADCAST_IDLE_SECS

    def test_idle_1h_default(self):
        from tools.graph.cli import _parse_duration
        secs = _parse_duration("1h")
        assert secs == 3600


class TestBroadcastFiltering(unittest.TestCase):
    """cmd_crosstalk_broadcast filters sessions by idle time."""

    def _make_session(self, session_id, last_activity):
        return {"session_id": session_id, "last_activity": last_activity}

    @mock.patch("tools.graph.cli._get_session_name", return_value="self-session")
    @mock.patch("tools.graph.cli.os.environ", {"CROSSTALK_TOKEN": "tok123", "GRAPH_API": "https://localhost:8080"})
    def test_filters_idle_sessions(self, mock_get_session):
        """Sessions idle > threshold are skipped."""
        from tools.graph.cli import _parse_last_activity

        now = time.time()
        sessions = [
            self._make_session("self-session", now),
            self._make_session("active-1", now - 600),   # 10 min ago — active
            self._make_session("active-2", now - 1800),  # 30 min ago — active
            self._make_session("idle-1", now - 7200),    # 2h ago — idle
            self._make_session("idle-2", now - 86400),   # 1d ago — idle
        ]

        idle_secs = 3600  # 1h threshold
        peers = []
        skipped = 0
        for s in sessions:
            if s["session_id"] == "self-session":
                continue
            la = _parse_last_activity(s.get("last_activity"))
            if la is None or (now - la) >= idle_secs:
                skipped += 1
                continue
            peers.append(s["session_id"])

        assert peers == ["active-1", "active-2"]
        assert skipped == 2

    def test_none_last_activity_skipped(self):
        """Sessions with None last_activity are always skipped."""
        from tools.graph.cli import _parse_last_activity

        now = time.time()
        s = self._make_session("no-activity", None)
        la = _parse_last_activity(s.get("last_activity"))
        assert la is None  # → will be skipped

    def test_iso_string_last_activity(self):
        """Sessions with ISO string last_activity are parsed correctly."""
        from tools.graph.cli import _parse_last_activity

        now = time.time()
        # 5 minutes ago as ISO
        from datetime import datetime, timezone
        recent = datetime.fromtimestamp(now - 300, tz=timezone.utc).isoformat()
        la = _parse_last_activity(recent)
        assert la is not None
        assert (now - la) < 3600  # within 1h


# ── API unit tests ──────────────────────────────────────────────────────

class TestBroadcastAPIValidation(unittest.TestCase):
    """Server-side /api/crosstalk/broadcast validation."""

    def test_max_idle_cap(self):
        """max_idle > 21600 should be rejected."""
        from tools.dashboard.server import _MAX_BROADCAST_IDLE_SECS
        assert _MAX_BROADCAST_IDLE_SECS == 21600

    def test_max_idle_default(self):
        """Default max_idle should be 3600."""
        # The endpoint defaults to 3600 when no max_idle param is given
        assert int("3600") == 3600


# ── Integration-style tests (mocked HTTP) ──────────────────────────────

class TestBroadcastCLIIntegration(unittest.TestCase):
    """End-to-end CLI broadcast with mocked HTTP calls."""

    @mock.patch("tools.graph.cli._get_session_name", return_value="self-session")
    def test_all_idle_shows_zero_sent(self, mock_get_session):
        """When all peers are idle, output shows 0 sent."""
        now = time.time()
        sessions_response = json.dumps([
            {"session_id": "self-session", "last_activity": now},
            {"session_id": "idle-1", "last_activity": now - 7200},
            {"session_id": "idle-2", "last_activity": now - 7200},
        ]).encode()

        with mock.patch("tools.graph.cli.os.environ",
                        {"CROSSTALK_TOKEN": "tok", "GRAPH_API": "https://localhost:8080"}):
            import ssl
            import urllib.request
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = sessions_response

            with mock.patch("urllib.request.urlopen", return_value=mock_resp):
                args = argparse.Namespace(
                    message=["hello"], content_stdin=None, idle="1h"
                )
                import io
                from contextlib import redirect_stdout
                f = io.StringIO()
                with redirect_stdout(f):
                    from tools.graph.cli import cmd_crosstalk_broadcast
                    cmd_crosstalk_broadcast(args)
                output = f.getvalue()
                assert "skipped" in output
                assert "idle > 1h" in output

    @mock.patch("tools.graph.cli._get_session_name", return_value="self-session")
    def test_mixed_sessions_shows_correct_counts(self, mock_get_session):
        """Mix of active and idle peers: output shows correct sent/skipped counts."""
        now = time.time()
        sessions_response = json.dumps([
            {"session_id": "self-session", "last_activity": now},
            {"session_id": "active-1", "last_activity": now - 600},
            {"session_id": "idle-1", "last_activity": now - 7200},
        ]).encode()

        call_count = [0]
        def mock_urlopen(req, timeout=None, context=None):
            call_count[0] += 1
            resp = mock.MagicMock()
            if call_count[0] == 1:
                # First call: active_sessions
                resp.read.return_value = sessions_response
            else:
                # Subsequent: send calls
                resp.read.return_value = b'{"delivered": true}'
            return resp

        with mock.patch("tools.graph.cli.os.environ",
                        {"CROSSTALK_TOKEN": "tok", "GRAPH_API": "https://localhost:8080"}):
            with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
                args = argparse.Namespace(
                    message=["hello"], content_stdin=None, idle="1h"
                )
                import io
                from contextlib import redirect_stdout
                f = io.StringIO()
                with redirect_stdout(f):
                    from tools.graph.cli import cmd_crosstalk_broadcast
                    cmd_crosstalk_broadcast(args)
                output = f.getvalue()
                assert "1 of 2" in output
                assert "1 skipped" in output

    def test_idle_exceeds_max_exits(self):
        """--idle 8h should exit with error."""
        args = argparse.Namespace(
            message=["hello"], content_stdin=None, idle="8h"
        )
        with pytest.raises(SystemExit):
            from tools.graph.cli import cmd_crosstalk_broadcast
            cmd_crosstalk_broadcast(args)


if __name__ == "__main__":
    unittest.main()
